import frappe
from woocommerce import API


def update_stock_levels_for_woocommerce_item(doc, method):
	if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
		if doc.doctype == "Sales Invoice":
			if doc.update_stock == 0:
				return
		item_codes = [row.item_code for row in doc.items]
		for item_code in item_codes:
			frappe.enqueue(
				"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
				enqueue_after_commit=True,
				item_code=item_code,
			)


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code):
	"""
	Updates stock levels of an item on all its associated WooCommerce sites.

	This function fetches the item from the database, then for each associated
	WooCommerce site, it retrieves the current inventory, calculates the new stock quantity,
	and posts the updated stock levels back to the WooCommerce site.
	"""
	item = frappe.get_doc("Item", item_code)

	if len(item.woocommerce_servers) == 0:
		return False
	else:
		wc_additional_settings = frappe.get_single("WooCommerce Additional Settings")

		bins = frappe.get_list(
			"Bin", {"item_code": item_code}, ["name", "warehouse", "reserved_qty", "actual_qty"]
		)

		for wc_site in item.woocommerce_servers:
			woocommerce_id = wc_site.woocommerce_id
			woocommerce_site = wc_site.woocommerce_site

			wc_server = next(
				(
					server
					for server in wc_additional_settings.servers
					if woocommerce_site in server.woocommerce_server_url and server.enable_sync
				),
				None,
			)

			if not wc_server:
				continue

			wc_api = API(
				url=wc_server.woocommerce_server_url,
				consumer_key=wc_server.api_consumer_key,
				consumer_secret=wc_server.api_consumer_secret,
				version="wc/v3",
				timeout=40,
			)

			data_to_post = {"stock_quantity": sum(bin.actual_qty for bin in bins)}

			try:
				response = wc_api.put(endpoint=f"products/{woocommerce_id}", data=data_to_post)
			except Exception as err:
				error_message = frappe.get_traceback() + "\n\nData in PUT request: \n" + str(data_to_post)
				frappe.log_error("WooCommerce Error", error_message)
				return False
			if response.status_code != 200:
				error_message = (
					"Status Code not 200\n\nData in PUT request: \n"
					+ str(data_to_post)
					+ "\n\nResponse: \n"
					+ response
				)
				frappe.log_error("WooCommerce Error", error_message)

		return True
