#!/usr/bin/env python3
import os
import time
import logging
import sys
from helpers import INTERVAL_TIME, GCP_PROJECT_ID, DRY_RUN, VERBOSE, get_settings_for_metrics, is_integer_or_float, print_human_readable_volume_dict
from helpers import convert_bytes_to_storage, scale_up_pvc, test_gmp_connection, describe_all_pvcs, send_kubernetes_event
from helpers import fetch_pvcs_from_gmp, printHeaderAndConfiguration, calculateBytesToScaleTo, GracefulKiller, cache
from gmp_client import GMPClient
from prometheus_client import start_http_server, Summary, Gauge, Counter, Info
import slack
import traceback

# Configure logging
log_level = logging.DEBUG if VERBOSE else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Initialize our metrics (counters)
METRICS = {}
METRICS['resize_evaluated']  = Counter('volume_autoscaler_resize_evaluated',  'Counter which is increased every time we evaluate resizing PVCs')
METRICS['resize_attempted']  = Counter('volume_autoscaler_resize_attempted',  'Counter which is increased every time we attempt to resize')
METRICS['resize_successful'] = Counter('volume_autoscaler_resize_successful', 'Counter which is increased every time we successfully resize')
METRICS['resize_failure']    = Counter('volume_autoscaler_resize_failure',    'Counter which is increased every time we fail to resize')
# Initialize our metrics (gauges)
METRICS['num_valid_pvcs'] = Gauge('volume_autoscaler_num_valid_pvcs', 'Gauge with the number of valid PVCs detected which we found to consider for scaling')
METRICS['num_valid_pvcs'].set(0)
METRICS['num_pvcs_above_threshold'] = Gauge('volume_autoscaler_num_pvcs_above_threshold', 'Gauge with the number of PVCs detected above the desired percentage threshold')
METRICS['num_pvcs_above_threshold'].set(0)
METRICS['num_pvcs_below_threshold'] = Gauge('volume_autoscaler_num_pvcs_below_threshold', 'Gauge with the number of PVCs detected below the desired percentage threshold')
METRICS['num_pvcs_below_threshold'].set(0)
# Initialize our metrics (info/settings)
METRICS['info'] = Info('volume_autoscaler_release', 'Release/version information about this volume autoscaler service')
METRICS['info'].info({'version': '2.0.0-gmp'})
METRICS['settings'] = Info('volume_autoscaler_settings', 'Settings currently used in this service')
METRICS['settings'].info(get_settings_for_metrics())

# Other globals
MAIN_LOOP_TIME = 1

# Entry point and main application loop
if __name__ == "__main__":

    # Initialize GMP client and test connection
    if not GCP_PROJECT_ID:
        logger.error("GCP_PROJECT_ID must be set or detectable from metadata service")
        exit(-1)
    
    logger.info("Initializing GMP client for project: %s", GCP_PROJECT_ID)
    gmp_client = GMPClient(GCP_PROJECT_ID)
    test_gmp_connection(gmp_client)

    # Startup our metrics endpoint
    logger.info("Starting metrics server on port 8000")
    start_http_server(8000)

    # TODO: Test k8s access, or just test on-the-fly below?

    # Reporting our configuration to the end-user
    printHeaderAndConfiguration()

    # Setup our graceful handling of kubernetes signals
    logger.info("Setting up signal handlers for graceful shutdown")
    killer = GracefulKiller()
    last_run = 0

    # Our main run loop, now using a signal handler to handle kubernetes signals gracefully (not mid-loop)
    while not killer.kill_now:

        # If it's not our interval time yet, only run once every INTERVAL_TIME seconds.  This extra bit helps us handle signals gracefully quicker
        if int(time.time()) - last_run <= INTERVAL_TIME:
            time.sleep(MAIN_LOOP_TIME)
            continue
        last_run = int(time.time())

        # In every loop, fetch all our pvcs state from Kubernetes
        try:
            METRICS['resize_evaluated'].inc()
            pvcs_in_kubernetes = describe_all_pvcs(simple=True)
        except Exception as e:
            logger.error("Exception while trying to describe all PVCs: %s", str(e), exc_info=True)
            time.sleep(MAIN_LOOP_TIME)
            continue

        # Fetch our volume usage from GMP
        try:
            pvcs_in_gmp = fetch_pvcs_from_gmp(gmp_client)
            logger.info("Found %d valid PVCs to assess in Google Managed Prometheus", len(pvcs_in_gmp))
            METRICS['num_valid_pvcs'].set(len(pvcs_in_gmp))
        except Exception as e:
            logger.error("Exception while trying to fetch PVC metrics from Google Managed Prometheus: %s", str(e), exc_info=True)
            time.sleep(MAIN_LOOP_TIME)
            continue

        # Iterate through every item and handle it accordingly
        METRICS['num_pvcs_above_threshold'].set(0)  # Reset these each loop
        METRICS['num_pvcs_below_threshold'].set(0)  # Reset these each loop
        for item in pvcs_in_gmp:
            try:
                volume_name = str(item['metric']['persistentvolumeclaim'])
                volume_namespace = str(item['metric']['namespace'])
                volume_description = "{}.{}".format(item['metric']['namespace'], item['metric']['persistentvolumeclaim'])
                volume_used_percent = int(item['value'][1])

                # Precursor check to ensure we have info for this pvc in kubernetes object
                if volume_description not in pvcs_in_kubernetes:
                    logger.error("Volume %s was not found in Kubernetes but had metrics in GMP. May be deleted or experiencing jitter.", volume_description)
                    continue

                pvcs_in_kubernetes[volume_description]['volume_used_percent'] = volume_used_percent
                try:
                    volume_used_inode_percent = int(item['value_inodes'])
                except:
                    volume_used_inode_percent = -1
                pvcs_in_kubernetes[volume_description]['volume_used_inode_percent'] = volume_used_inode_percent

                if VERBOSE:
                    logger.debug("Volume %s: %d%% disk used of %s, %d%% inodes used",
                                volume_description,
                                volume_used_percent,
                                pvcs_in_kubernetes[volume_description]['volume_size_status'],
                                volume_used_inode_percent if volume_used_inode_percent > -1 else 0)
                    print_human_readable_volume_dict(pvcs_in_kubernetes[volume_description])

                # Check if we are NOT in an alert condition
                if volume_used_percent < pvcs_in_kubernetes[volume_description]['scale_above_percent'] and volume_used_inode_percent < pvcs_in_kubernetes[volume_description]['scale_above_percent']:
                    METRICS['num_pvcs_below_threshold'].inc()
                    cache.unset(volume_description)
                    if VERBOSE:
                        logger.debug("Volume %s is below threshold (%d%%)", volume_description, pvcs_in_kubernetes[volume_description]['scale_above_percent'])
                    continue
                else:
                    METRICS['num_pvcs_above_threshold'].inc()

                # If we are in alert condition, record this in our simple in-memory counter
                if cache.get(volume_description):
                    cache.set(volume_description, cache.get(volume_description) + 1)
                else:
                    cache.set(volume_description, 1)

                # Incase we aren't verbose, and didn't print this above, now that we're in alert we will print this
                if not VERBOSE:
                    print("Volume {} is {}% in-use of the {} available".format(volume_description,volume_used_percent,pvcs_in_kubernetes[volume_description]['volume_size_status']))
                    print("Volume {} is {}% inode in-use".format(volume_description,volume_used_inode_percent))

                # Print the alert status and reason
                if volume_used_percent >= pvcs_in_kubernetes[volume_description]['scale_above_percent']:
                    print("  BECAUSE it has space used above {}%".format(pvcs_in_kubernetes[volume_description]['scale_above_percent']))
                elif volume_used_inode_percent >= pvcs_in_kubernetes[volume_description]['scale_above_percent']:
                    print("  BECAUSE it has inodes used above {}%".format(pvcs_in_kubernetes[volume_description]['scale_above_percent']))
                print("  ALERT has been for {} period(s) which needs to at least {} period(s) to scale".format(cache.get(volume_description), pvcs_in_kubernetes[volume_description]['scale_after_intervals']))

                # Check if we are NOT in a possible scale condition
                if cache.get(volume_description) < pvcs_in_kubernetes[volume_description]['scale_after_intervals']:
                    print("  BUT need to wait for {} intervals in alert before considering to scale".format( pvcs_in_kubernetes[volume_description]['scale_after_intervals'] ))
                    print("  FYI this has desired_size {} and current size {}".format( convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_spec_bytes']), convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'])))
                    print("=============================================================================================================")
                    continue

                # If we are in a possible scale condition, check if we recently scaled it and handle accordingly
                if pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time'] >= int(time.mktime(time.gmtime())):
                    print("  BUT need to wait {} seconds to scale since the last scale time {} seconds ago".format( abs(pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time']) - int(time.mktime(time.gmtime())), abs(pvcs_in_kubernetes[volume_description]['last_resized_at'] - int(time.mktime(time.gmtime()))) ))
                    print("=============================================================================================================")
                    continue

                # If we reach this far then we will be scaling the disk, all preconditions were passed from above
                if pvcs_in_kubernetes[volume_description]['last_resized_at'] == 0:
                    print("  AND we need to scale it immediately, it has never been scaled previously")
                else:
                    print("  AND we need to scale it immediately, it last scaled {} seconds ago".format( abs((pvcs_in_kubernetes[volume_description]['last_resized_at'] + pvcs_in_kubernetes[volume_description]['scale_cooldown_time']) - int(time.mktime(time.gmtime()))) ))

                # Calculate how many bytes to resize to based on the parameters provided globally and per-this pv annotations
                resize_to_bytes = calculateBytesToScaleTo(
                    original_size     = pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'],
                    scale_up_percent  = pvcs_in_kubernetes[volume_description]['scale_up_percent'],
                    min_increment     = pvcs_in_kubernetes[volume_description]['scale_up_min_increment'],
                    max_increment     = pvcs_in_kubernetes[volume_description]['scale_up_max_increment'],
                    maximum_size      = pvcs_in_kubernetes[volume_description]['scale_up_max_size'],
                )
                # TODO: Check here if storage class has the ALLOWVOLUMEEXPANSION flag set to true, read the SC from pvcs_in_kubernetes[volume_description]['storage_class'] ?

                # If our resize bytes failed for some reason, eg putting invalid data into the annotations on the PV
                if resize_to_bytes == False:
                    print("-------------------------------------------------------------------------------------------------------------")
                    print("  Error/Exception while trying to determine what to resize to, volume causing failure:")
                    print("-------------------------------------------------------------------------------------------------------------")
                    print(pvcs_in_kubernetes[volume_description])
                    print("=============================================================================================================")
                    continue

                # If our resize bytes is less than our original size (because the user set the max-bytes to something too low)
                if resize_to_bytes < pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']:
                    print("-------------------------------------------------------------------------------------------------------------")
                    print("  Error/Exception while trying to scale this up.  Is it possible your maximum SCALE_UP_MAX_SIZE is too small?")
                    print("-------------------------------------------------------------------------------------------------------------")
                    print("   Maximum Size: {} ({})".format(pvcs_in_kubernetes[volume_description]['scale_up_max_size'], convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['scale_up_max_size'])))
                    print("  Original Size: {} ({})".format(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'], convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes'])))
                    print("      Resize To: {} ({})".format(resize_to_bytes, convert_bytes_to_storage(resize_to_bytes)))
                    print("-------------------------------------------------------------------------------------------------------------")
                    print(" Volume causing failure:")
                    print_human_readable_volume_dict(pvcs_in_kubernetes[volume_description])
                    print("=============================================================================================================")
                    continue

                # Check if we are already at the max volume size (either globally, or this-volume specific)
                if resize_to_bytes == pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']:
                    print("  SKIPPING scaling this because we are at the maximum size of {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['scale_up_max_size'])))
                    print("=============================================================================================================")
                    continue

                # Check if we set on this PV we want to ignore the volume autoscaler
                if pvcs_in_kubernetes[volume_description]['ignore']:
                    print("  IGNORING scaling this because the ignore annotation was set to true")
                    print("=============================================================================================================")
                    continue

                # Lets debounce this incase we did this resize last interval(s)
                if cache.get(f"{volume_description}-has-been-resized"):
                    print("  DEBOUNCING and skipping this scaling, we resized within recent intervals")
                    print("=============================================================================================================")
                    continue

                # Check if we are DRY-RUN-ing and won't do anything
                if DRY_RUN:
                    print("  DRY RUN was set, but we would have resized this disk from {} to {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']), convert_bytes_to_storage(resize_to_bytes)))
                    print("=============================================================================================================")
                    continue

                # If we aren't dry-run, lets resize
                METRICS['resize_attempted'].inc()
                print("  RESIZING disk from {} to {}".format(convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']), convert_bytes_to_storage(resize_to_bytes)))
                status_output = "to scale up `{}` by `{}%` from `{}` to `{}`, it was using more than `{}%` disk or inode space over the last `{} seconds`".format(
                    volume_description,
                    pvcs_in_kubernetes[volume_description]['scale_up_percent'],
                    convert_bytes_to_storage(pvcs_in_kubernetes[volume_description]['volume_size_status_bytes']),
                    convert_bytes_to_storage(resize_to_bytes),
                    pvcs_in_kubernetes[volume_description]['scale_above_percent'],
                    cache.get(volume_description) * INTERVAL_TIME
                )
                # Send event that we're starting to request a resize
                send_kubernetes_event(
                    name=volume_name, namespace=volume_namespace, reason="VolumeResizeRequested",
                    message="Requesting {}".format(status_output)
                )

                if scale_up_pvc(volume_namespace, volume_name, resize_to_bytes):
                    METRICS['resize_successful'].inc()
                    # Save this to cache for debouncing
                    cache.set(f"{volume_description}-has-been-resized", True)
                    # Print success to console
                    status_output = "Successfully requested {}".format(status_output)
                    print(status_output)
                    # Intentionally skipping sending an event to Kubernetes on success, the above event is enough for now until we detect if resize succeeded
                    # Print success to Slack
                    if slack.SLACK_WEBHOOK_URL and len(slack.SLACK_WEBHOOK_URL) > 0:
                        print(f"Sending slack message to {slack.SLACK_CHANNEL}")
                        slack.send(status_output)
                else:
                    METRICS['resize_failure'].inc()
                    # Print failure to console
                    status_output = "FAILED requesting {}".format(status_output)
                    print(status_output)
                    # Print failure to Kubernetes Events
                    send_kubernetes_event(
                        name=volume_name, namespace=volume_namespace, reason="VolumeResizeRequestFailed",
                        message=status_output, type="Warning"
                    )
                    # Print failure to Slack
                    if slack.SLACK_WEBHOOK_URL and len(slack.SLACK_WEBHOOK_URL) > 0:
                        print(f"Sending slack message to {slack.SLACK_CHANNEL}")
                        slack.send(status_output, severity="error")

            except Exception:
                print("Exception caught while trying to process record")
                print(item)
                traceback.print_exc()

            if VERBOSE:
                print("=============================================================================================================")

        # Wait until our next interval
        time.sleep(MAIN_LOOP_TIME)

    print("We were sent a signal handler to kill, exited gracefully")
    exit(0)
