import time
from collections import defaultdict
from typing import List, Optional

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion

from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)


def update_item_price_for_woocommerce_item_from_hook(doc, method):
	if not frappe.flags.in_test:
		if doc.doctype == "Item Price" and not getattr(doc.flags, "in_sync", False):
			frappe.enqueue(
				"woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
				enqueue_after_commit=True,
				item_code=doc.item_code,
				item_price_doc=doc,
			)


@frappe.whitelist()
def run_item_price_sync_in_background():
	servers = frappe.get_all(
		"WooCommerce Server",
		filters={"enable_sync": 1, "enable_price_list_sync": 1, "enable_scheduled_price_sync": 1},
		fields=["name"],
	)
	if not servers:
		return
	frappe.enqueue(run_item_price_sync, queue="long", timeout=3600)


@frappe.whitelist()
def run_item_price_sync(
	item_code: Optional[str] = None, item_price_doc: Optional[ItemPrice] = None
):
	sync = SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc)
	sync.run()
	return True


class SynchroniseItemPrice(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of ERPNext Items with WooCommerce Products via Batch API
	"""

	item_code: Optional[str]
	item_price_list: List

	def __init__(
		self,
		servers: List[WooCommerceServer | frappe._dict] = None,
		item_code: Optional[str] = None,
		item_price_doc: Optional[ItemPrice] = None,
	) -> None:
		super().__init__(servers)
		self.item_code = item_code
		self.item_price_doc = item_price_doc
		self.wc_server = None
		self.item_price_list = []

	def run(self) -> None:
		"""
		Run synchronisation with concurrency locking
		"""
		for server in self.servers:
			self.wc_server = server
			self.get_erpnext_item_prices()
			if not self.item_price_list:
				continue

			# Distributed Redis lock to prevent multiple concurrent sync workers
			lock_name = f"wc_price_sync_{self.wc_server.name}"
			try:
				with frappe.cache().lock(lock_name, timeout=3600):
					self.sync_items_with_woocommerce_products()
			except Exception as e:
				if "lock" in str(e).lower():
					frappe.logger().warning(
						f"WooCommerce Price Sync for {self.wc_server.name} skipped: another sync job is currently in progress."
					)
				else:
					frappe.log_error(f"WooCommerce Price Sync Error: {self.wc_server.name}", frappe.get_traceback())
					raise e

	def get_erpnext_item_prices(self) -> None:
		"""
		Get list of ERPNext Item Prices to synchronise
		"""
		self.item_price_list = []
		if (
			self.wc_server.enable_sync
			and self.wc_server.enable_price_list_sync
			and self.wc_server.price_list
		):
			ip = qb.DocType("Item Price")
			iwc = qb.DocType("Item WooCommerce Server")
			item = qb.DocType("Item")
			and_conditions = []
			and_conditions.append(ip.price_list == self.wc_server.price_list)
			and_conditions.append(iwc.woocommerce_server == self.wc_server.name)
			and_conditions.append(item.disabled == 0)
			and_conditions.append(iwc.woocommerce_id.isnotnull())
			and_conditions.append(iwc.enabled == 1)
			if self.item_code:
				and_conditions.append(ip.item_code == self.item_code)

			self.item_price_list = (
				qb.from_(ip)
				.inner_join(iwc)
				.on(iwc.parent == ip.item_code)
				.inner_join(item)
				.on(item.name == ip.item_code)
				.select(ip.name, ip.item_code, ip.price_list_rate, iwc.woocommerce_server, iwc.woocommerce_id)
				.where(Criterion.all(and_conditions))
				.run(as_dict=True)
			)

	def sync_items_with_woocommerce_products(self) -> None:
		"""
		Synchronise Item Prices with WooCommerce Products using WooCommerce Batch API (up to 100 items per request)
		"""
		if not self.item_price_list:
			return

		wc_api = APIWithRequestLogging(
			url=self.wc_server.woocommerce_server_url,
			consumer_key=self.wc_server.api_consumer_key,
			consumer_secret=self.wc_server.api_consumer_secret,
			version="wc/v3",
			timeout=40,
			verify_ssl=True,
		)

		item_codes = list(set([row.item_code for row in self.item_price_list]))

		# Gather variant info to properly direct variant vs simple product updates
		variant_info = frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "variant_of"],
		)
		variant_map = {v.name: v.variant_of for v in variant_info}

		# Look up parent WooCommerce IDs for variants
		parent_items = list(set([v.variant_of for v in variant_info if v.variant_of]))
		parent_woo_map = {}
		if parent_items:
			parent_wc_rows = frappe.get_all(
				"Item WooCommerce Server",
				filters={
					"parent": ["in", parent_items],
					"woocommerce_server": self.wc_server.name,
					"enabled": 1,
				},
				fields=["parent", "woocommerce_id"],
			)
			for row in parent_wc_rows:
				if row.woocommerce_id and row.parent:
					parent_woo_map[row.parent] = str(row.woocommerce_id)

		simple_updates = []
		variations_by_parent = defaultdict(list)

		for item_price in self.item_price_list:
			price_list_rate = (
				self.item_price_doc.price_list_rate
				if self.item_price_doc and self.item_price_doc.price_list == self.wc_server.price_list
				else item_price.price_list_rate
			)

			rate_val = float(price_list_rate or 0)
			if rate_val <= 0:
				update_entry = {
					"id": int(item_price.woocommerce_id),
					"status": "draft",
					"regular_price": "0",
				}
			else:
				update_entry = {
					"id": int(item_price.woocommerce_id),
					"regular_price": str(price_list_rate),
				}
				if self.wc_server.new_product_publish_status:
					update_entry["status"] = self.wc_server.new_product_publish_status

			variant_of = variant_map.get(item_price.item_code)
			parent_woo_id = parent_woo_map.get(variant_of) if variant_of else None

			if parent_woo_id:
				variations_by_parent[parent_woo_id].append(update_entry)
			else:
				simple_updates.append(update_entry)

		batch_size = int(self.wc_server.get("batch_size") or 100)
		batch_delay = float(self.wc_server.get("batch_delay") or 0.5) if not self.item_code else 0.0

		# 1. Update simple / top-level products via Batch API
		if simple_updates:
			for i in range(0, len(simple_updates), batch_size):
				chunk = simple_updates[i : i + batch_size]
				payload = {"update": chunk}
				try:
					res = wc_api.post("products/batch", data=payload)
					if res.status_code != 200:
						frappe.log_error(
							"WooCommerce Price Batch Update Error",
							f"Status {res.status_code}: {res.text[:500]}\nPayload count: {len(chunk)}",
						)
				except Exception:
					frappe.log_error("WooCommerce Price Batch Exception", frappe.get_traceback())
				if batch_delay > 0:
					time.sleep(batch_delay)

		# 2. Update variations grouped by parent via Batch API
		for parent_id, variations in variations_by_parent.items():
			for i in range(0, len(variations), batch_size):
				chunk = variations[i : i + batch_size]
				payload = {"update": chunk}
				try:
					res = wc_api.post(f"products/{parent_id}/variations/batch", data=payload)
					if res.status_code != 200:
						# Fallback to simple products batch if parent endpoint rejects
						res_fallback = wc_api.post("products/batch", data=payload)
						if res_fallback.status_code != 200:
							frappe.log_error(
								"WooCommerce Variation Price Batch Error",
								f"Parent {parent_id}, Status {res.status_code}: {res.text[:500]}",
							)
				except Exception:
					frappe.log_error("WooCommerce Variation Price Batch Exception", frappe.get_traceback())
				if batch_delay > 0:
					time.sleep(batch_delay)
