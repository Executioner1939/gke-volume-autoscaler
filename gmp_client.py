#!/usr/bin/env python3
"""
Google Managed Prometheus (GMP) Client
Handles authentication and queries to Google Cloud Monitoring's Prometheus API
"""
import os
import requests
import google.auth
import google.auth.transport.requests
from google.auth import compute_engine
import traceback
import logging

logger = logging.getLogger(__name__)


class GMPClient:
    """Client for interacting with Google Managed Prometheus API"""
    
    def __init__(self, project_id=None):
        """
        Initialize GMP client with project ID and authentication
        
        Args:
            project_id: GCP project ID. If None, will attempt to auto-detect
        """
        self.project_id = project_id or self._detect_project_id()
        if not self.project_id:
            raise ValueError("GCP_PROJECT_ID must be set or detectable from metadata service")
            
        self.base_url = f"https://monitoring.googleapis.com/v1/projects/{self.project_id}/location/global/prometheus/api/v1"
        
        # Use Application Default Credentials (Workload Identity in GKE)
        self.credentials, _ = google.auth.default(
            scopes=[
                'https://www.googleapis.com/auth/cloud-platform',
                'https://www.googleapis.com/auth/monitoring',
                'https://www.googleapis.com/auth/monitoring.read'
            ]
        )
        self.auth_req = google.auth.transport.requests.Request()
        
        logger.info("Initialized GMP client for project: %s", self.project_id)
    
    def _detect_project_id(self):
        """
        Attempt to detect GCP project ID from environment or metadata service
        
        Returns:
            str: Project ID or None if not detectable
        """
        # Try environment variable first
        project_id = os.getenv('GCP_PROJECT_ID')
        if project_id:
            logger.debug("Found GCP project ID from environment: %s", project_id)
            return project_id
        
        # Try to get from metadata service (works in GKE)
        try:
            logger.debug("Attempting to get project ID from metadata service")
            import urllib.request
            req = urllib.request.Request(
                'http://metadata.google.internal/computeMetadata/v1/project/project-id',
                headers={'Metadata-Flavor': 'Google'}
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                project_id = response.read().decode('utf-8')
                logger.debug("Found GCP project ID from metadata service: %s", project_id)
                return project_id
        except Exception as e:
            logger.debug("Metadata service not available: %s", str(e))
            pass
        
        logger.warning("Could not detect GCP project ID")
        return None
    
    def _get_headers(self):
        """
        Get authentication headers for API requests
        
        Returns:
            dict: Headers with Bearer token
        """
        # Refresh token if needed
        logger.debug("Refreshing authentication token")
        self.credentials.refresh(self.auth_req)
        return {
            'Authorization': f'Bearer {self.credentials.token}',
            'Content-Type': 'application/json'
        }
    
    def query(self, promql_query, timeout=15):
        """
        Execute a PromQL query against Google Managed Prometheus
        
        Args:
            promql_query: The PromQL query string
            timeout: Request timeout in seconds
            
        Returns:
            dict: JSON response from the API
            
        Raises:
            Exception: If the query fails
        """
        url = f"{self.base_url}/query"
        params = {'query': promql_query}
        
        logger.debug("Executing GMP query: %s", promql_query)
        
        try:
            response = requests.get(
                url, 
                params=params, 
                headers=self._get_headers(),
                timeout=timeout
            )
            
            logger.debug("GMP query response status: %d", response.status_code)
            
            if response.status_code != 200:
                error_msg = f"GMP query failed with status {response.status_code}"
                try:
                    error_detail = response.json()
                    if 'error' in error_detail:
                        error_msg += f": {error_detail['error']}"
                except:
                    error_msg += f": {response.text}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            result = response.json()
            
            # Validate response structure
            if 'status' in result and result['status'] != 'success':
                error_msg = f"Query failed: {result.get('error', 'Unknown error')}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            logger.debug("GMP query completed successfully")
            return result
            
        except requests.exceptions.Timeout:
            error_msg = f"GMP query timed out after {timeout} seconds"
            logger.error(error_msg)
            raise Exception(error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"GMP query request failed: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
    
    def test_connection(self):
        """
        Test if we can successfully query GMP
        
        Returns:
            bool: True if connection successful
        """
        try:
            logger.debug("Testing GMP connection with 'up' query")
            # Simple query to test connectivity
            result = self.query('up', timeout=5)
            success = 'data' in result
            if success:
                logger.debug("GMP connection test successful")
            else:
                logger.warning("GMP connection test failed: no data in response")
            return success
        except Exception as e:
            logger.error("GMP connection test failed: %s", str(e), exc_info=True)
            return False