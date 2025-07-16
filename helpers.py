from os import getenv          # Environment variable handling
import time                    # Sleep/time
import datetime
import requests                # For making HTTP requests to GMP
import kubernetes              # For talking to the Kubernetes API
from kubernetes.client import ApiException
# packaging import removed - no longer needed for version checking
import signal                  # For sigkill handling
import random                  # Random string generation
import traceback               # Debugging/trace outputs
import slack                   # For sending slack messages
import logging

logger = logging.getLogger(__name__)

# Used below in init variables
def detect_gcp_project_id():
    """Detect GCP project ID from environment or metadata service"""
    # Try environment variable first
    project_id = getenv('GCP_PROJECT_ID')
    if project_id:
        return project_id
    
    # Try to get from metadata service (works in GKE)
    try:
        import urllib.request
        req = urllib.request.Request(
            'http://metadata.google.internal/computeMetadata/v1/project/project-id',
            headers={'Metadata-Flavor': 'Google'}
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.read().decode('utf-8')
    except Exception:
        # Metadata service not available
        pass
    
    return None

# Input/configuration variables
INTERVAL_TIME = int(getenv('INTERVAL_TIME') or 60)                               # How often (in seconds) to scan GMP for checking if we need to resize
SCALE_ABOVE_PERCENT = int(getenv('SCALE_ABOVE_PERCENT') or 80)                   # What percent out of 100 the volume must be consuming before considering to scale it
SCALE_AFTER_INTERVALS = int(getenv('SCALE_AFTER_INTERVALS') or 5)                # How many intervals of INTERVAL_TIME a volume must be above SCALE_ABOVE_PERCENT before we scale
SCALE_UP_PERCENT = int(getenv('SCALE_UP_PERCENT') or 20)                         # How much percent of the current volume size to scale up by.  eg: 100 == (if disk is 10GB, scale to 20GB), eg: 20 == (if disk is 10GB, scale to 12GB)
SCALE_UP_MIN_INCREMENT = int(getenv('SCALE_UP_MIN_INCREMENT') or 1000000000)     # How many bytes is the minimum that we can resize up by, default is 1GB (in bytes, so 1000000000)
SCALE_UP_MAX_INCREMENT = int(getenv('SCALE_UP_MAX_INCREMENT') or 16000000000000) # How many bytes is the maximum that we can resize up by, default is 16TB (in bytes, so 16000000000000)
SCALE_UP_MAX_SIZE = int(getenv('SCALE_UP_MAX_SIZE') or 16000000000000)           # How many bytes is the maximum disk size that we can resize up, default is 16TB for EBS volumes in AWS (in bytes, so 16000000000000)
SCALE_COOLDOWN_TIME = int(getenv('SCALE_COOLDOWN_TIME') or 22200)                # How long (in seconds) we must wait before scaling this volume again.  For AWS EBS, this is 6 hours which is 21600 seconds but for good measure we add an extra 10 minutes to this, so 22200
GCP_PROJECT_ID = getenv('GCP_PROJECT_ID') or detect_gcp_project_id()             # GCP project ID for Google Managed Prometheus
DRY_RUN = True if getenv('DRY_RUN', "false").lower() == "true" else False        # If we want to dry-run this
GMP_LABEL_MATCH = getenv('GMP_LABEL_MATCH') or ''                              # A PromQL label query to restrict volumes for this to see and scale, without braces.  eg: 'namespace="dev"'
HTTP_TIMEOUT = int(getenv('HTTP_TIMEOUT', "15")) or 15                           # Allows to set the timeout for calls to GMP and Kubernetes.  This might be needed if your GMP or Kubernetes is over a remote WAN link with high latency and/or is heavily loaded
VERBOSE = True if getenv('VERBOSE', "false").lower() == "true" else False        # If we want to verbose mode


# Simple helper to pass back
def get_settings_for_metrics():
    return {
        'interval_time_seconds': str(INTERVAL_TIME),
        'scale_above_percent': str(SCALE_ABOVE_PERCENT),
        'scale_after_intervals': str(SCALE_AFTER_INTERVALS),
        'scale_up_percent': str(SCALE_UP_PERCENT),
        'scale_up_minimum_increment_bytes': str(SCALE_UP_MIN_INCREMENT),
        'scale_up_maximum_increment_bytes': str(SCALE_UP_MAX_INCREMENT),
        'scale_up_maximum_size_bytes': str(SCALE_UP_MAX_SIZE),
        'scale_cooldown_time_seconds': str(SCALE_COOLDOWN_TIME),
        'gcp_project_id': GCP_PROJECT_ID if GCP_PROJECT_ID else 'not-set',
        'dry_run': "true" if DRY_RUN else "false",
        'gmp_label_match': GMP_LABEL_MATCH,
        'gmp_mode': 'true',
        'http_timeout_seconds': str(HTTP_TIMEOUT),
        'verbose_enabled': "true" if VERBOSE else "false",
    }

# Headers are now handled by GMP client
headers = {}

# This handler helps handle sigint/term gracefully (not in the middle of an runloop)
class GracefulKiller:
  kill_now = False
  def __init__(self):
    signal.signal(signal.SIGINT, self.exit_gracefully)
    signal.signal(signal.SIGTERM, self.exit_gracefully)

  def exit_gracefully(self, *args):
    self.kill_now = True


# Setup a cache helper for caching and expiring things with TTLs, used for debouncing
class Cache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self.cache = {}

    def set(self, key, value, ttl=False):
        expiration = time.time() + self.ttl
        if ttl != False:
            expiration = time.time() + ttl
        self.cache[key] = (value, expiration)

    def get(self, key):
        if key in self.cache:
            value, expiration = self.cache[key]
            if time.time() < expiration:
                return value
            else:
                del self.cache[key]
        return None

    def unset(self, key):
        if key in self.cache:
            del self.cache[key]

    def reset(self):
        self.cache = {}

# Note: We want the TTL time to be 10x the interval time by default to ensure items in it
#       last through a few intervals incase of jitter and for debouncing volume changes
cache = Cache(ttl=INTERVAL_TIME * 10)


#############################
# Initialize Kubernetes
#############################
try:
    # First, try to use in-cluster config, aka run inside of Kubernetes
    kubernetes.config.load_incluster_config()
except Exception as e:
    try:
        # If we aren't running in kubernetes, try to use the kubectl config file as a fallback
        kubernetes.config.load_kube_config()
    except Exception as ex:
        raise ex
kubernetes_core_api  = kubernetes.client.CoreV1Api()


#############################
# Helper functions
#############################
# Simple header printing before the program starts, prints the variables this is configured for at runtime
def printHeaderAndConfiguration():
    logger.info("Volume Autoscaler Configuration:")
    logger.info("  Mode: Google Managed Prometheus (GMP)")
    logger.info("  GCP Project ID: %s", GCP_PROJECT_ID if GCP_PROJECT_ID else 'not-set')
    logger.info("  Label selector: {%s}", GMP_LABEL_MATCH)
    logger.info("  Query interval: %d seconds", INTERVAL_TIME)
    logger.info("  Scale after: %d intervals (%d seconds total)", SCALE_AFTER_INTERVALS, SCALE_AFTER_INTERVALS * INTERVAL_TIME)
    logger.info("  Scale when disk over: %d%%", SCALE_ABOVE_PERCENT)
    logger.info("  Scale up by: %d%% of current size", SCALE_UP_PERCENT)
    logger.info("  Min increment: %s", convert_bytes_to_storage(SCALE_UP_MIN_INCREMENT))
    logger.info("  Max increment: %s", convert_bytes_to_storage(SCALE_UP_MAX_INCREMENT))
    logger.info("  Max size: %s", convert_bytes_to_storage(SCALE_UP_MAX_SIZE))
    logger.info("  Cooldown period: %d seconds", SCALE_COOLDOWN_TIME)
    logger.info("  Verbose mode: %s", "ENABLED" if VERBOSE else "disabled")
    logger.info("  Dry run: %s", "ENABLED (no scaling will occur)" if DRY_RUN else "disabled")
    logger.info("  HTTP timeout: %d seconds", HTTP_TIMEOUT)
    logger.info("  Slack notifications: %s", "ENABLED" if len(slack.SLACK_WEBHOOK_URL) > 0 else "disabled")
    if len(slack.SLACK_WEBHOOK_URL) > 0:
        logger.info("    Slack channel: %s", slack.SLACK_CHANNEL)
        if slack.SLACK_MESSAGE_PREFIX:
            logger.info("    Message prefix: %s", slack.SLACK_MESSAGE_PREFIX)
        if slack.SLACK_MESSAGE_SUFFIX:
            logger.info("    Message suffix: %s", slack.SLACK_MESSAGE_SUFFIX)


# Figure out how many bytes to scale to based on the original size, scale up percent, minimum increment and maximum size
def calculateBytesToScaleTo(original_size, scale_up_percent, min_increment, max_increment, maximum_size):
    try:
        resize_to_bytes = int((original_size * (0.01 * scale_up_percent)) + original_size)
        logger.debug("Calculated initial resize from %s to %s (%d%% increase)", 
                    convert_bytes_to_storage(original_size), convert_bytes_to_storage(resize_to_bytes), scale_up_percent)
        
        # Check if resize bump is too small
        if resize_to_bytes - original_size < min_increment:
            # Using default scale up if too small
            resize_to_bytes = original_size + min_increment
            logger.debug("Resize increment too small, using minimum increment. New size: %s", 
                        convert_bytes_to_storage(resize_to_bytes))

        # Check if resize bump is too large
        if resize_to_bytes - original_size > max_increment:
            # Using default scale up if too large
            resize_to_bytes = original_size + max_increment
            logger.debug("Resize increment too large, using maximum increment. New size: %s", 
                        convert_bytes_to_storage(resize_to_bytes))

        # Now check if it is too large overall (max disk size)
        if resize_to_bytes > maximum_size:
            resize_to_bytes = maximum_size
            logger.debug("Resize would exceed maximum size, capping at: %s", 
                        convert_bytes_to_storage(resize_to_bytes))

        # Now check if we're already maxed (16TB?) then we don't need to complete this scale activity
        if original_size == resize_to_bytes:
            logger.debug("Volume already at maximum size, cannot resize further")
            return False

        # If we're good, send back our resizeto byets
        logger.debug("Final resize calculation: %s -> %s", 
                    convert_bytes_to_storage(original_size), convert_bytes_to_storage(resize_to_bytes))
        return resize_to_bytes
    except Exception as e:
        logger.error("Exception calculating bytes to scale to: %s", str(e), exc_info=True)
        return False

# Check if is integer or float
def is_integer_or_float(n):
    try:
        float(n)
    except ValueError:
        return False
    else:
        return float(n).is_integer()

# Convert the K8s storage size definitions (eg: 10G, 5Ti, etc) into number of bytes
def convert_storage_to_bytes(storage):
    logger.debug("Converting storage size '%s' to bytes", storage)

    # BinarySI == Ki | Mi | Gi | Ti | Pi | Ei
    if storage.endswith('Ki'):
        return int(storage.replace("Ki","")) * 1024
    if storage.endswith('Mi'):
        return int(storage.replace("Mi","")) * 1024 * 1024
    if storage.endswith('Gi'):
        return int(storage.replace("Gi","")) * 1024 * 1024 * 1024
    if storage.endswith('Ti'):
        return int(storage.replace("Ti","")) * 1024 * 1024 * 1024 * 1024
    if storage.endswith('Pi'):
        return int(storage.replace("Pi","")) * 1024 * 1024 * 1024 * 1024 * 1024
    if storage.endswith('Ei'):
        return int(storage.replace("Ei","")) * 1024 * 1024 * 1024 * 1024 * 1024 * 1024

    # decimalSI == m | k | M | G | T | P | E | "" (this last one is the fallthrough at the end)
    if storage.endswith('k'):
        return int(storage.replace("k","")) * 1000
    if storage.endswith('K'):
        return int(storage.replace("K","")) * 1000
    if storage.endswith('m'):
        return int(storage.replace("m","")) * 1000 * 1000
    if storage.endswith('M'):
        return int(storage.replace("M","")) * 1000 * 1000
    if storage.endswith('G'):
        return int(storage.replace("G","")) * 1000 * 1000 * 1000
    if storage.endswith('T'):
        return int(storage.replace("T","")) * 1000 * 1000 * 1000 * 1000
    if storage.endswith('P'):
        return int(storage.replace("P","")) * 1000 * 1000 * 1000 * 1000 * 1000
    if storage.endswith('E'):
        return int(storage.replace("E","")) * 1000 * 1000 * 1000 * 1000 * 1000 * 1000

    # decimalExponent == e | E (in the middle of two integers)
    lowercaseDecimalExponent = storage.split('e')
    uppercaseDecimalExponent = storage.split('E')
    if len(lowercaseDecimalExponent) > 1 or len(uppercaseDecimalExponent) > 1:
        return int(float(str(format(float(storage)))))

    # If none above match, then it should just be an integer value (in bytes)
    return int(storage)


# Try a numeric format to see if it's close enough (within 10 percent, aka 0.1) to the definition
def try_numeric_format(bytes, size_multiplier, suffix, match_by_percentage = 0.1):
    # If bytes is too small, right away exit
    if bytes < (size_multiplier - (size_multiplier * match_by_percentage)):
        return False
    try_result = round(bytes / size_multiplier)
    # print("try_result = {}".format(try_result))
    retest_value = try_result * size_multiplier
    # print("retest_value = {}".format(retest_value))
    difference = abs(retest_value - bytes)
    # print("difference = {}".format(difference))
    if difference < (bytes * 0.1):
        return "{}{}".format(try_result, suffix)
    return False


# Convert bytes (int) to an "sexY" kubernetes storage definition (10G, 5Ti, etc)
# TODO?: If possible, add hinting of which to try first, base10 or base2, based on what was used previously to get closer to the right amount
def convert_bytes_to_storage(bytes):

    # Todo: Add Petabytes/Exobytes?

    # Ensure its an intger
    bytes = int(bytes)

    # First, we'll try all base10 values...
    # Check if we can convert this into terrabytes
    result = try_numeric_format(bytes, 1000000000000, 'T')
    if result:
        return result

    # Check if we can convert this into gigabytes
    result = try_numeric_format(bytes, 1000000000, 'G')
    if result:
        return result

    # Check if we can convert this into megabytes
    result = try_numeric_format(bytes, 1000000, 'M')
    if result:
        return result

    # Do we ever use things this small?  For now going to skip this...
    # result = try_numeric_format(bytes, 1000, 'k')
    # if result:
    #     return result

    # Next, we'll try all base2 values...
    result = try_numeric_format(bytes, 1099511627776, 'Ti')
    if result:
        return result

    # Next, we'll try all base2 values...
    result = try_numeric_format(bytes, 1073741824, 'Gi')
    if result:
        return result

    # Next, we'll try all base2 values...
    result = try_numeric_format(bytes, 1048576, 'Mi')
    if result:
        return result

    # # Do we ever use things this small?  For now going to skip this...
    # result = try_numeric_format(bytes, 1024, 'Ki')
    # if result:
    #     return result

    # Worst-case just return bytes, a non-sexy value
    return bytes


# The PVC definition from Kubernetes has tons of variables in various maps of maps of maps, simplify
# it to a flat dict for the values we care about, along with allowing per-pvc overrides from annotations
def convert_pvc_to_simpler_dict(pvc):
    return_dict = {}
    return_dict['name'] = pvc.metadata.name
    try:
        return_dict['volume_size_spec'] = pvc.spec.resources.requests['storage']
    except:
        return_dict['volume_size_spec'] = "0"
        logger.debug("PVC %s.%s has no storage spec", pvc.metadata.namespace, pvc.metadata.name)
    return_dict['volume_size_spec_bytes'] = convert_storage_to_bytes(return_dict['volume_size_spec'])
    try:
        return_dict['volume_size_status'] = pvc.status.capacity['storage']
    except:
        return_dict['volume_size_status'] = "0"
        logger.debug("PVC %s.%s has no storage status", pvc.metadata.namespace, pvc.metadata.name)
    return_dict['volume_size_status_bytes'] = convert_storage_to_bytes(return_dict['volume_size_status'])
    return_dict['namespace'] = pvc.metadata.namespace
    try:
        return_dict['storage_class'] = pvc.spec.storage_class_name
    except:
        return_dict['storage_class'] = ""
    try:
        return_dict['resource_version'] = pvc.metadata.resource_version
    except:
        return_dict['resource_version'] = ""
    try:
        return_dict['uid'] = pvc.metadata.uid
    except:
        return_dict['uid'] = ""

    # Set our defaults
    return_dict['last_resized_at']        = 0
    return_dict['scale_above_percent']    = SCALE_ABOVE_PERCENT
    return_dict['scale_after_intervals']  = SCALE_AFTER_INTERVALS
    return_dict['scale_up_percent']       = SCALE_UP_PERCENT
    return_dict['scale_up_min_increment'] = SCALE_UP_MIN_INCREMENT
    return_dict['scale_up_max_increment'] = SCALE_UP_MAX_INCREMENT
    return_dict['scale_up_max_size']      = SCALE_UP_MAX_SIZE
    return_dict['scale_cooldown_time']    = SCALE_COOLDOWN_TIME
    return_dict['ignore']                 = False

    # Override defaults with annotations on the PVC
    try:
        if 'volume.autoscaler.kubernetes.io/last-resized-at' in pvc.metadata.annotations:
            return_dict['last_resized_at'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/last-resized-at'])
    except Exception as e:
        logger.warning("Could not convert last_resized_at to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-above-percent' in pvc.metadata.annotations:
            return_dict['scale_above_percent'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-above-percent'])
    except Exception as e:
        logger.warning("Could not convert scale_above_percent to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-after-intervals' in pvc.metadata.annotations:
            return_dict['scale_after_intervals'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-after-intervals'])
    except Exception as e:
        logger.warning("Could not convert scale_after_intervals to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-up-percent' in pvc.metadata.annotations:
            return_dict['scale_up_percent'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-up-percent'])
    except Exception as e:
        logger.warning("Could not convert scale_up_percent to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-up-min-increment' in pvc.metadata.annotations:
            return_dict['scale_up_min_increment'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-up-min-increment'])
    except Exception as e:
        logger.warning("Could not convert scale_up_min_increment to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-up-max-increment' in pvc.metadata.annotations:
            return_dict['scale_up_max_increment'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-up-max-increment'])
    except Exception as e:
        logger.warning("Could not convert scale_up_max_increment to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-up-max-size' in pvc.metadata.annotations:
            return_dict['scale_up_max_size'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-up-max-size'])
    except Exception as e:
        logger.warning("Could not convert scale_up_max_size to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/scale-cooldown-time' in pvc.metadata.annotations:
            return_dict['scale_cooldown_time'] = int(pvc.metadata.annotations['volume.autoscaler.kubernetes.io/scale-cooldown-time'])
    except Exception as e:
        logger.warning("Could not convert scale_cooldown_time to int for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    try:
        if 'volume.autoscaler.kubernetes.io/ignore' in pvc.metadata.annotations and pvc.metadata.annotations['volume.autoscaler.kubernetes.io/ignore'].lower() == "true":
            return_dict['ignore'] = True
            logger.debug("PVC %s.%s has ignore annotation set to true", pvc.metadata.namespace, pvc.metadata.name)
    except Exception as e:
        logger.warning("Could not convert ignore to bool for PVC %s.%s: %s", 
                      pvc.metadata.namespace, pvc.metadata.name, str(e))

    # Return our cleaned up and simple flat dict with the values we care about, with overrides if specified
    return return_dict


# Describe all the PVCs in Kubernetes
# TODO: Check if we need to page this, and how well it handles scale (100+ PVCs, etc)
def describe_all_pvcs(simple=False):
    logger.debug("Fetching all PVCs from Kubernetes API")
    api_response = kubernetes_core_api.list_persistent_volume_claim_for_all_namespaces(timeout_seconds=HTTP_TIMEOUT)
    output_objects = {}
    for item in api_response.items:
        if simple:
            output_objects["{}.{}".format(item.metadata.namespace,item.metadata.name)] = convert_pvc_to_simpler_dict(item)
        else:
            output_objects["{}.{}".format(item.metadata.namespace,item.metadata.name)] = item

    logger.debug("Found %d PVCs in Kubernetes", len(output_objects))
    return output_objects


# Scale up an PVC in Kubernetes
def scale_up_pvc(namespace, name, new_size):
    try:
        logger.info("Scaling PVC %s.%s to %s", namespace, name, convert_bytes_to_storage(new_size))
        
        result = kubernetes_core_api.patch_namespaced_persistent_volume_claim(
                    name=name,
                    namespace=namespace,
                    body={
                        "metadata": {"annotations": {"volume.autoscaler.kubernetes.io/last-resized-at": str(int(time.mktime(time.gmtime())))}},
                        "spec": {"resources": {"requests": {"storage": new_size}} }
                    }
                )

        actual_size = convert_storage_to_bytes(result.spec.resources.requests['storage'])
        logger.info("  Desired size: %s", convert_bytes_to_storage(new_size))
        logger.info("  Actual size: %s", convert_bytes_to_storage(actual_size))

        # If the new size is within' 10% of the desired size.  This is necessary because of the megabyte/mebibyte issue
        if abs(actual_size - new_size) < (new_size * 0.1):
            logger.info("Successfully scaled PVC %s.%s", namespace, name)
            return result
        else:
            raise Exception("New size did not take for some reason")

    except Exception as e:
        logger.error("Failed to scale PVC %s.%s to %s: %s", namespace, name, convert_bytes_to_storage(new_size), str(e))
        return False


# Test if GMP is accessible
def test_gmp_connection(gmp_client):
    """Test if we can successfully connect to Google Managed Prometheus"""
    try:
        logger.debug("Testing connection to Google Managed Prometheus")
        if gmp_client.test_connection():
            logger.info("Successfully connected to Google Managed Prometheus")
            return True
        else:
            logger.error("Failed to connect to Google Managed Prometheus")
            return False
    except Exception as e:
        logger.error("Cannot access Google Managed Prometheus: %s", str(e), exc_info=True)
        exit(-1)


# Get a list of PVCs from Google Managed Prometheus with their metrics of disk usage
def fetch_pvcs_from_gmp(gmp_client, label_match=GMP_LABEL_MATCH):
    """Fetch PVC metrics from Google Managed Prometheus"""
    
    # Query for disk usage percentage
    disk_query = "ceil((1 - kubelet_volume_stats_available_bytes{{ {} }} / kubelet_volume_stats_capacity_bytes)*100)".format(label_match)
    
    logger.debug("Querying GMP for disk usage metrics")
    try:
        disk_response = gmp_client.query(disk_query, timeout=HTTP_TIMEOUT)
        
        if 'data' not in disk_response or 'result' not in disk_response['data']:
            logger.error("Unexpected response format from GMP disk query")
            return []
        
        disk_results = disk_response['data']['result']
        logger.debug("Found %d volumes with disk metrics", len(disk_results))
        
    except Exception as e:
        logger.error("Failed to query disk metrics from GMP: %s", str(e), exc_info=True)
        return []
    
    # Query for inode usage percentage
    output_response_object = []
    try:
        inode_query = "ceil((1 - kubelet_volume_stats_inodes_free{{ {} }} / kubelet_volume_stats_inodes)*100)".format(label_match)
        inode_response = gmp_client.query(inode_query, timeout=HTTP_TIMEOUT)
        
        # Prepare values to merge/inject with our first response list/array above
        inject_values = {}
        if 'data' in inode_response and 'result' in inode_response['data']:
            for item in inode_response['data']['result']:
                ourkey = "{}_{}".format(item['metric']['namespace'], item['metric']['persistentvolumeclaim'])
                inject_values[ourkey] = item['value'][1]
            logger.debug("Found %d volumes with inode metrics", len(inject_values))
        
        # Process and merge disk and inode results
        for item in disk_results:
            try:
                ourkey = "{}_{}".format(item['metric']['namespace'], item['metric']['persistentvolumeclaim'])
                if ourkey in inject_values:
                    item['value_inodes'] = inject_values[ourkey]
            except Exception as e:
                logger.error("Exception while trying to inject inode data: %s", str(e))
            output_response_object.append(item)
            
    except Exception as e:
        logger.warning("Failed to query inode metrics from GMP, continuing with disk metrics only: %s", str(e))
        # Even if inode query fails, return disk results
        output_response_object = disk_results
    
    return output_response_object


# Describe an specific PVC
def describe_pvc(namespace, name, simple=False):
    logger.debug("Describing PVC %s.%s", namespace, name)
    api_response = kubernetes_core_api.list_namespaced_persistent_volume_claim(namespace, limit=1, field_selector="metadata.name=" + name, timeout_seconds=HTTP_TIMEOUT)
    # print(api_response)
    for item in api_response.items:
        # If the user wants pre-parsed, making it a bit easier to work with than a huge map of map of maps
        if simple:
            return convert_pvc_to_simpler_dict(item)
        return item
    logger.error("No PVC found for %s.%s", namespace, name)
    raise Exception("No PVC found for {}:{}".format(namespace,name))


# Convert an PVC to an involved object for Kubernetes events
def get_involved_object_from_pvc(pvc):
    return kubernetes.client.V1ObjectReference(
        api_version="v1",
        kind="PersistentVolumeClaim",
        name=pvc.metadata.name,
        namespace=pvc.metadata.namespace,
        resource_version=pvc.metadata.resource_version,
        uid=pvc.metadata.uid,
    )

# Send events to Kubernetes.  This is used when we modify PVCs
def send_kubernetes_event(namespace, name, reason, message, type="Normal"):
    logger.debug("Sending Kubernetes event to %s.%s: %s", namespace, name, reason)
    try:
        # Lookup our PVC
        pvc = describe_pvc(namespace, name)

        # Generate our metadata and object relation for this event
        involved_object = get_involved_object_from_pvc(pvc)
        source = kubernetes.client.V1EventSource(component="volume-autoscaler")
        metadata = kubernetes.client.V1ObjectMeta(
            namespace=namespace,
            name=name + ''.join([random.choice('123456789abcdef') for n in range(16)]),
        )

        # Generate our event body with the reason and message set
        body = kubernetes.client.CoreV1Event(
                    involved_object=involved_object,
                    metadata=metadata,
                    reason=reason,
                    message=message,
                    type=type,
                    source=source,
                    first_timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
               )

        api_response = kubernetes_core_api.create_namespaced_event(namespace, body, field_manager="volume_autoscaler")
        logger.debug("Successfully sent Kubernetes event to %s.%s", namespace, name)
    except ApiException as e:
        logger.error("Exception when calling CoreV1Api->create_namespaced_event: %s", str(e))
    except:
        logger.error("Unexpected error while sending Kubernetes event", exc_info=True)

# Print a sexy human readable dict for volume
def print_human_readable_volume_dict(input_dict):
    for key in input_dict:
        print("    {}: {}".format(key.rjust(25), input_dict[key]), end='')
        if key in ['volume_size_spec','volume_size_spec_bytes','volume_size_status','volume_size_status_bytes','scale_up_min_increment','scale_up_max_increment','scale_up_max_size'] and is_integer_or_float(input_dict[key]):
            print(" ({})".format(convert_bytes_to_storage(input_dict[key])), end='')
        if key in ['scale_cooldown_time']:
            print(" ({})".format(time.strftime('%H:%M:%S', time.gmtime(input_dict[key]))), end='')
        if key in ['last_resized_at']:
            print(" ({})".format(time.strftime('%Y-%m-%d %H:%M:%S %Z %z', time.localtime(input_dict[key]))), end='')
        if key in ['scale_up_percent','scale_above_percent','volume_used_percent','volume_used_inode_percent']:
            print("%", end='')
        print("") # Newline
