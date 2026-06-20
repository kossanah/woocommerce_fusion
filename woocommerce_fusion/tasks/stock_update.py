import math
import time
from collections import defaultdict

import frappe

from woocommerce_fusion.tasks.utils import APIWithRequestLogging

verify_ssl = not frappe._dev_server


def update_stock_levels_for_woocommerce_item(doc, method):
	if not frappe.flags.in_test:
		if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
			# Check if there are any enabled WooCommerce Servers with stock sync enabled
			if (
				len(
					frappe.get_list(
						"WooCommerce Server", filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1}
					)
				)
				> 0
			):
				if doc.doctype == "Sales Invoice":
					if doc.update_stock == 0:
						return
				item_codes = [row.item_code for row in doc.items]
				if item_codes:
					frappe.enqueue(
						"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site_multiple",
						enqueue_after_commit=True,
						item_codes=list(set(item_codes)),
					)


def update_stock_levels_for_all_enabled_items_in_background():
	"""
	Get all enabled ERPNext Items and post stock updates to WooCommerce
	"""
	erpnext_items = []
	current_page_length = 500
	start = 0

	# Get all items, 500 records at a time
	while current_page_length == 500:
		items = frappe.db.get_all(
			doctype="Item",
			filters={"disabled": 0},
			fields=["name"],
			start=start,
			page_length=500,
		)
		erpnext_items.extend(items)
		current_page_length = len(items)
		start += current_page_length

	item_codes = [item.name for item in erpnext_items]
	if item_codes:
		frappe.enqueue(
			"woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site_multiple",
			item_codes=item_codes,
			queue="long",
		)


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code):
	"""
	Updates stock levels of an item on all its associated WooCommerce sites.
	"""
	return update_stock_levels_for_items([item_code])


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site_multiple(item_codes):
	"""
	Updates stock levels of multiple items in batch on their associated WooCommerce sites.
	"""
	return update_stock_levels_for_items(item_codes)


def update_stock_levels_for_items(item_codes):
	"""
	Updates stock levels for multiple items in batch on their associated WooCommerce servers.
	"""
	if not item_codes:
		return True

	# Deduplicate item_codes
	item_codes = list(set(item_codes))

	# Get all enabled WooCommerce Servers with stock level sync enabled
	wc_servers = frappe.get_all(
		"WooCommerce Server", filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1}
	)
	wc_servers = [frappe.get_doc("WooCommerce Server", server.name) for server in wc_servers]

	if not wc_servers:
		return True

	# Gather Bins for these items in one query
	bins = frappe.get_all(
		"Bin",
		filters={"item_code": ["in", item_codes]},
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
	)
	item_bins = defaultdict(list)
	for b in bins:
		item_bins[b.item_code].append(b)

	for wc_server in wc_servers:
		# Gather the mapping for the items on this server
		items_to_sync = frappe.db.sql(
			"""
			SELECT 
				itw.parent as item_code,
				itw.woocommerce_id,
				itw.enabled,
				it.variant_of,
				it.is_stock_item,
				it.disabled
			FROM 
				`tabItem WooCommerce Server` itw
			INNER JOIN 
				`tabItem` it ON it.name = itw.parent
			WHERE 
				itw.woocommerce_server = %s 
				AND itw.enabled = 1
				AND itw.woocommerce_id IS NOT NULL
				AND itw.woocommerce_id != ''
				AND it.is_stock_item = 1
				AND it.disabled = 0
				AND itw.parent IN %s
			""",
			(wc_server.name, item_codes),
			as_dict=True,
		)

		if not items_to_sync:
			continue

		wc_api = APIWithRequestLogging(
			url=wc_server.woocommerce_server_url,
			consumer_key=wc_server.api_consumer_key,
			consumer_secret=wc_server.api_consumer_secret,
			version="wc/v3",
			timeout=40,
			verify_ssl=verify_ssl,
		)

		target_warehouses = [row.warehouse for row in wc_server.warehouses]

		def get_stock_qty(item_code):
			item_bins_list = item_bins.get(item_code, [])
			total_qty = 0
			for b in item_bins_list:
				if b.warehouse in target_warehouses:
					qty = b.actual_qty
					if wc_server.subtract_reserved_stock:
						qty -= b.reserved_qty
					total_qty += qty
			return math.floor(total_qty)

		# Fetch parents for variants
		variant_woo_ids = [item.woocommerce_id for item in items_to_sync if item.variant_of]
		wc_product_parent_map = {}
		if variant_woo_ids:
			wc_products = frappe.get_all(
				"WooCommerce Product",
				filters={
					"woocommerce_server": wc_server.name,
					"woocommerce_id": ["in", variant_woo_ids],
				},
				fields=["woocommerce_id", "parent_id"],
			)
			wc_product_parent_map = {p.woocommerce_id: p.parent_id for p in wc_products if p.parent_id}

		parent_items = list(set([item.variant_of for item in items_to_sync if item.variant_of]))
		parent_item_map = {}
		if parent_items:
			parent_wc_servers = frappe.get_all(
				"Item WooCommerce Server",
				filters={
					"parent": ["in", parent_items],
					"woocommerce_server": wc_server.name,
					"enabled": 1,
				},
				fields=["parent", "woocommerce_id"],
			)
			parent_item_map = {p.parent: p.woocommerce_id for p in parent_wc_servers if p.woocommerce_id}

		def get_parent_id(item):
			p_id = wc_product_parent_map.get(item.woocommerce_id)
			if p_id:
				return str(p_id)
			p_id = parent_item_map.get(item.variant_of)
			if p_id:
				return str(p_id)
			return None

		simple_products_updates = []
		variations_by_parent = defaultdict(list)

		for item in items_to_sync:
			stock_qty = get_stock_qty(item.item_code)
			update_payload = {"id": item.woocommerce_id, "stock_quantity": stock_qty}

			if item.variant_of:
				parent_id = get_parent_id(item)
				if parent_id:
					variations_by_parent[parent_id].append(update_payload)
				else:
					simple_products_updates.append(update_payload)
			else:
				simple_products_updates.append(update_payload)

		# Perform batch updates
		# Simple / Variable Products
		if simple_products_updates:
			for i in range(0, len(simple_products_updates), 100):
				chunk = simple_products_updates[i : i + 100]
				payload = {"update": chunk}
				response = wc_api.post(endpoint="products/batch", data=payload)
				if response.status_code != 200:
					error_message = (
						f"Batch Update simple products failed (status: {response.status_code})\n\n"
						f"Response: {response.text}"
					)
					frappe.log_error("WooCommerce Batch Update Error", error_message)
					raise ValueError(error_message)
				time.sleep(0.5)

		# Variations (grouped by parent_id)
		for parent_id, variations in variations_by_parent.items():
			for i in range(0, len(variations), 100):
				chunk = variations[i : i + 100]
				payload = {"update": chunk}
				response = wc_api.post(endpoint=f"products/{parent_id}/variations/batch", data=payload)
				if response.status_code != 200:
					# Batch variation update failed. Fallback: try updating them as simple products
					frappe.logger().warning(
						f"WooCommerce Batch Variation Update failed for parent {parent_id} (status: {response.status_code}). "
						f"Attempting fallback to simple product batch update."
					)
					# Try simple product batch update fallback
					response_fallback = wc_api.post(endpoint="products/batch", data=payload)
					if response_fallback.status_code != 200:
						error_message = (
							f"Batch Update variations fallback failed (status: {response_fallback.status_code})\n\n"
							f"Response: {response_fallback.text}"
						)
						frappe.log_error("WooCommerce Batch Update Error", error_message)
						raise ValueError(error_message)
				time.sleep(0.5)

	return True
