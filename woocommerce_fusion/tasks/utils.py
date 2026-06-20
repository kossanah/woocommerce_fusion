import time
import traceback

import frappe
import requests
from frappe.utils.caching import redis_cache
from woocommerce import API


class APIWithRequestLogging(API):
	"""WooCommerce API with Request Logging."""

	def _API__request(self, method, endpoint, data, params=None, **kwargs):
		"""Override _request method to also create a 'WooCommerce Request Log'"""
		result = None
		max_attempts = 3
		for attempt in range(max_attempts):
			try:
				result = super()._API__request(method, endpoint, data, params, **kwargs)
				if result is not None and result.status_code == 429:
					retry_after = int(result.headers.get("Retry-After", 5 * (2 ** attempt)))
					frappe.logger().warning(
						f"WooCommerce API returned 429 Too Many Requests (endpoint: {endpoint}). "
						f"Sleeping for {retry_after} seconds before retry (attempt {attempt + 1}/{max_attempts})."
					)
					time.sleep(retry_after)
					continue
				break
			except Exception as e:
				if not frappe.flags.in_test and is_woocommerce_request_logging_enabled(self.url):
					frappe.enqueue(
						"woocommerce_fusion.tasks.utils.log_woocommerce_request",
						url=self.url,
						endpoint=endpoint,
						request_method=method,
						params=params,
						data=data,
						res=result,
						traceback="".join(traceback.format_stack(limit=8)),
					)
				raise e

		if not frappe.flags.in_test and is_woocommerce_request_logging_enabled(self.url):
			frappe.enqueue(
				"woocommerce_fusion.tasks.utils.log_woocommerce_request",
				url=self.url,
				endpoint=endpoint,
				request_method=method,
				params=params,
				data=data,
				res=result,
				traceback="".join(traceback.format_stack(limit=8)),
			)
		return result


@redis_cache(ttl=86400)
def is_woocommerce_request_logging_enabled(woocommerce_server_url: str) -> bool:
	"""
	Checks if WooCommerce request logging is enabled for the given WooCommerce server URL.
	Args:
	        woocommerce_server_url (str): The URL of the WooCommerce server.
	Returns:
	        bool: True if request logging is enabled, False otherwise.
	"""
	enabled = frappe.get_all(
		"WooCommerce Server",
		filters={"woocommerce_server_url": woocommerce_server_url},
		fields=["enable_woocommerce_request_logs"],
	)
	if not enabled:
		return False
	return enabled[0].enable_woocommerce_request_logs


def log_woocommerce_request(
	url: str,
	endpoint: str,
	request_method: str,
	params: dict,
	data: dict,
	res: requests.Response | None = None,
	traceback: str = None,
):
	request_log = frappe.get_doc(
		{
			"doctype": "WooCommerce Request Log",
			"user": frappe.session.user if frappe.session.user else None,
			"url": url,
			"endpoint": endpoint,
			"method": request_method,
			"params": frappe.as_json(params) if params else None,
			"data": frappe.as_json(data) if data else None,
			"response": f"{str(res)}\n{res.text}" if res is not None else None,
			"error": frappe.get_traceback(),
			"status": "Success" if res and res.status_code in [200, 201] else "Error",
			"time_elapsed": res.elapsed.total_seconds() if res is not None else None,
		}
	)

	request_log.save(ignore_permissions=True)
