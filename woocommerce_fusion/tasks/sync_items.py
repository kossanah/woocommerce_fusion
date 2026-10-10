import urllib.parse
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import frappe
from erpnext.stock.doctype.item.item import Item
from frappe import ValidationError, _, _dict
from frappe.query_builder import Criterion
from frappe.utils import get_datetime, now
from jsonpath_ng.ext import parse

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.tasks.sync import SynchroniseWooCommerce
from woocommerce_fusion.tasks.utils import APIWithRequestLogging
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)
from woocommerce_fusion.woocommerce.woocommerce_api import (
	WC_RESOURCE_DELIMITER,
	generate_woocommerce_record_name_from_domain_and_id,
)



def safe_int(val, default=None):
	try:
		if val is None or str(val).strip().lower() in ("none", "", "null"):
			return default
		return int(val)
	except (ValueError, TypeError):
		return default

def run_item_sync_from_hook(doc, method):
	"""
	Intended to be triggered by a Document Controller hook from Item.
	Guard against recursive hook triggers during sync.
	"""
	if frappe.flags.in_test or getattr(frappe.flags, "in_sync", False):
		return

	if (
		doc.doctype == "Item"
		and not doc.flags.get("created_by_sync", False)
		and not doc.flags.get("in_sync", False)
		and len(doc.woocommerce_servers) > 0
	):
		# If this is a simple item (no variants) with no existing WooCommerce ID,
		# check if it has a valid price > 0 before enqueueing sync
		if not doc.has_variants:
			has_existing_wc_id = any(s.woocommerce_id for s in doc.woocommerce_servers if s.enabled)
			if not has_existing_wc_id:
				has_valid_price = False
				for s in doc.woocommerce_servers:
					if s.enabled:
						wc_server = frappe.get_cached_doc("WooCommerce Server", s.woocommerce_server)
						if wc_server.enable_price_list_sync and wc_server.price_list:
							rate = frappe.db.get_value(
								"Item Price",
								{"item_code": doc.name, "price_list": wc_server.price_list},
								"price_list_rate",
							)
							if rate and float(rate) > 0:
								has_valid_price = True
								break
						else:
							has_valid_price = True
							break
				if not has_valid_price:
					frappe.msgprint(
						_("Sync to WooCommerce skipped for {0}: item has 0 or no price in price list.").format(
							frappe.bold(doc.name)
						),
						indicator="orange",
						alert=True,
					)
					return

		frappe.msgprint(
			_("Background sync to WooCommerce triggered for {0} {1}").format(frappe.bold(doc.name), method),
			indicator="blue",
			alert=True,
		)
		frappe.enqueue(
			clear_sync_hash_and_run_item_sync,
			item_code=doc.name,
			job_id=f"sync_item::{doc.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)


@frappe.whitelist()
def run_item_sync(
	item_code: Optional[str] = None,
	item: Optional[Item] = None,
	woocommerce_product_name: Optional[str] = None,
	woocommerce_product: Optional[WooCommerceProduct] = None,
	enqueue: bool = False,
	force_push: bool = False,
) -> Tuple[Optional[Item], Optional[WooCommerceProduct]]:
	"""
	Helper function that prepares arguments for item sync.
	Guards with frappe.flags.in_sync to prevent recursive hooks during sync execution.
	"""
	# Validate inputs, at least one of the parameters should be provided
	if not any([item_code, item, woocommerce_product_name, woocommerce_product]):
		raise ValueError(
			"At least one of item_code, item, woocommerce_product_name, woocommerce_product parameters required"
		)

	frappe.flags.in_sync = True
	sync = None
	try:
		# Get ERPNext Item and WooCommerce product if they exist
		if woocommerce_product or woocommerce_product_name:
			if not woocommerce_product:
				woocommerce_product = frappe.get_doc(
					{"doctype": "WooCommerce Product", "name": woocommerce_product_name}
				)
				woocommerce_product.load_from_db()

			# Trigger sync
			sync = SynchroniseItem(woocommerce_product=woocommerce_product, force_push=force_push)
			if enqueue:
				frappe.enqueue(
					sync.run,
					job_id=f"sync_wc_product::{woocommerce_product.name}",
					deduplicate=True,
				)
			else:
				sync.run()

		elif item or item_code:
			if not item:
				item = frappe.get_doc("Item", item_code)
			if not item.woocommerce_servers:
				frappe.throw(_("No WooCommerce Servers defined for Item {0}").format(item_code))
			for wc_server in item.woocommerce_servers:
				if not wc_server.enabled:
					continue
				# Trigger sync for enabled linked server
				sync = SynchroniseItem(
					item=ERPNextItemToSync(item=item, item_woocommerce_server_idx=wc_server.idx),
					force_push=force_push,
				)
				if enqueue:
					frappe.enqueue(
						sync.run,
						job_id=f"sync_item_run::{item.name}",
						deduplicate=True,
					)
				else:
					sync.run()

		return (
			sync.item.item if sync and sync.item else None,
			sync.woocommerce_product if sync else None,
		)
	finally:
		frappe.flags.in_sync = False


def sync_woocommerce_products_modified_since(date_time_from=None):
	"""
	Get list of WooCommerce products modified since date_time_from.
	Only executes if enable_scheduled_item_sync is enabled on at least one WooCommerce Server.
	Throttles requests and protects with a distributed Redis lock.
	"""
	# Check if any enabled WooCommerce Server has scheduled item sync enabled
	enabled_servers = frappe.get_all(
		"WooCommerce Server",
		filters={"enable_sync": 1, "enable_scheduled_item_sync": 1},
		fields=["name"],
	)
	if not enabled_servers:
		return

	wc_settings = frappe.get_doc("WooCommerce Integration Settings")

	if not date_time_from:
		date_time_from = wc_settings.wc_last_sync_date_items

	# Validate
	if not date_time_from:
		error_text = _(
			"'Last Items Syncronisation Date' field on 'WooCommerce Integration Settings' is missing"
		)
		frappe.log_error(
			"WooCommerce Items Sync Task Error",
			error_text,
		)
		raise ValueError(error_text)

	lock_name = "wc_scheduled_items_sync"
	try:
		with frappe.cache().lock(lock_name, timeout=3600):
			wc_products = get_list_of_wc_products(date_time_from=date_time_from)
			for wc_product in wc_products:
				try:
					run_item_sync(woocommerce_product=wc_product, enqueue=False)
					time.sleep(0.5)
				# Skip items with errors, as these exceptions will be logged
				except Exception:
					pass

			frappe.db.set_single_value("WooCommerce Settings", "wc_last_sync_date_items", now())
	except Exception as e:
		if "lock" in str(e).lower():
			frappe.logger().warning(
				"WooCommerce scheduled items sync skipped: another sync job is currently in progress."
			)
		else:
			frappe.log_error("WooCommerce Scheduled Items Sync Error", frappe.get_traceback())


def format_erpnext_img_url(image_details) -> Optional[str]:
	"""
	Return a publicly accessible URL for an ERPNext file, or None if the file is private
	or unavailable. Safe-encodes spaces and special characters.

	image_details is a tuple/list from frappe.db.get_value with fields:
	  [0] file_name, [1] file_url, [2] is_private, [3] content_hash, [4] modified
	"""
	if image_details[2] == 0:  # is_private == 0 means the file is publicly accessible
		file_url = image_details[1]
		if file_url:
			if file_url.startswith("/"):
				# Relative URL — prepend the site URL and encode spaces
				site_url = frappe.utils.get_url()
				quoted_path = urllib.parse.quote(file_url, safe="/:")
				return f"{site_url.rstrip('/')}{quoted_path}"
			return urllib.parse.quote(file_url, safe="/:?=&")
	return None


@dataclass
class ERPNextItemToSync:
	"""Class for keeping track of an ERPNext Item and the relevant WooCommerce Server to sync to"""

	item: Item
	item_woocommerce_server_idx: int

	@property
	def item_woocommerce_server(self):
		return self.item.woocommerce_servers[self.item_woocommerce_server_idx - 1]


class SynchroniseItem(SynchroniseWooCommerce):
	"""
	Class for managing synchronisation of WooCommerce Product with ERPNext Item
	"""

	def __init__(
		self,
		servers: List[WooCommerceServer | _dict] = None,
		item: Optional[ERPNextItemToSync] = None,
		woocommerce_product: Optional[WooCommerceProduct] = None,
		force_push: bool = False,
	) -> None:
		super().__init__(servers)
		self.item = item
		self.woocommerce_product = woocommerce_product
		self.force_push = force_push
		self.settings = frappe.get_cached_doc("WooCommerce Integration Settings")

	def run(self):
		"""
		Run synchronisation
		"""
		try:
			self.get_corresponding_item_or_product()
			self.sync_wc_product_with_erpnext_item()
		except SyncDisabledError:
			return
		except Exception as err:
			try:
				woocommerce_product_dict = (
					self.woocommerce_product.as_dict()
					if isinstance(self.woocommerce_product, WooCommerceProduct)
					else self.woocommerce_product
				)
			except Exception:
				woocommerce_product_dict = self.woocommerce_product
			error_message = f"{frappe.get_traceback()}\n\nItem Data: \n{str(self.item) if self.item else ''}\n\nWC Product Data \n{str(woocommerce_product_dict) if self.woocommerce_product else ''})"
			frappe.log_error("WooCommerce Error", error_message)
			raise err

	def get_corresponding_item_or_product(self):
		"""
		If we have an ERPNext Item, get the corresponding WooCommerce Product.
		Uses direct record load_from_db instead of slow paginated searches that miss variations.
		"""
		if (
			self.item and not self.woocommerce_product and self.item.item_woocommerce_server.woocommerce_id
		):
			# Validate that this Item's WooCommerce Server has sync enabled
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)
			if not wc_server.enable_sync:
				raise SyncDisabledError(wc_server)

			server_name = self.item.item_woocommerce_server.woocommerce_server
			wc_id = self.item.item_woocommerce_server.woocommerce_id
			wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
				domain=server_name, resource_id=wc_id
			)
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product", "name": wc_product_name})

			try:
				wc_product.load_from_db()
				self.woocommerce_product = wc_product
			except frappe.DoesNotExistError:
				# Clear stale ID and associated sync metadata ONLY if product is truly gone on WooCommerce (404)
				frappe.db.set_value(
					"Item WooCommerce Server",
					self.item.item_woocommerce_server.name,
					{
						"woocommerce_id": None,
						"woocommerce_last_sync_hash": None,
						"woocommerce_image_id": None,
						"woocommerce_last_image_url": None,
					},
					update_modified=False,
				)
				self.item.item_woocommerce_server.woocommerce_id = None
				self.woocommerce_product = None

		elif (
			self.item
			and not self.woocommerce_product
			and not self.item.item_woocommerce_server.woocommerce_id
			and self.item.item.item_code
		):
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.item.item_woocommerce_server.woocommerce_server
			)
			if wc_server.enable_sync:
				# Before attempting to create a new product, check if a product with this SKU already exists on WooCommerce
				sku = self.item.item.item_code
				server_name = self.item.item_woocommerce_server.woocommerce_server
				try:
					wc_api = APIWithRequestLogging(
						url=wc_server.woocommerce_server_url,
						consumer_key=wc_server.api_consumer_key,
						consumer_secret=wc_server.api_consumer_secret,
						version="wc/v3",
						timeout=40,
					)
					res = wc_api.get("products", params={"sku": sku})
					if res.status_code == 200:
						matching_products = res.json()
						if isinstance(matching_products, list) and len(matching_products) > 0:
							found_wc_id = str(matching_products[0]["id"])
							wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
								domain=server_name, resource_id=found_wc_id
							)
							wc_product = frappe.get_doc(
								{"doctype": "WooCommerce Product", "name": wc_product_name}
							)
							wc_product.load_from_db()
							self.woocommerce_product = wc_product
							frappe.db.set_value(
								"Item WooCommerce Server",
								self.item.item_woocommerce_server.name,
								"woocommerce_id",
								found_wc_id,
								update_modified=False,
							)
							self.item.item_woocommerce_server.woocommerce_id = found_wc_id
							frappe.logger("woocommerce_fusion").info(
								f"Found existing WooCommerce product #{found_wc_id} by SKU '{sku}' for Item '{self.item.item.name}'"
							)
				except Exception as sku_err:
					frappe.logger("woocommerce_fusion").warning(
						f"SKU pre-lookup failed for Item '{self.item.item.name}': {sku_err}"
					)

		if self.woocommerce_product and not self.item:
			self.get_erpnext_item()

	def get_erpnext_item(self):
		"""
		Get erpnext item for a WooCommerce Product
		"""
		if not all(
			[self.woocommerce_product.woocommerce_server, self.woocommerce_product.woocommerce_id]
		):
			raise ValueError("Both woocommerce_server and woocommerce_id required")

		iws = frappe.qb.DocType("Item WooCommerce Server")
		itm = frappe.qb.DocType("Item")

		and_conditions = [
			iws.woocommerce_server == self.woocommerce_product.woocommerce_server,
			iws.woocommerce_id == self.woocommerce_product.woocommerce_id,
		]

		item_codes = (
			frappe.qb.from_(iws)
			.join(itm)
			.on(iws.parent == itm.name)
			.where(Criterion.all(and_conditions))
			.select(iws.parent, iws.name)
			.limit(1)
		).run(as_dict=True)

		found_item = frappe.get_doc("Item", item_codes[0].parent) if item_codes else None
		if found_item:
			self.item = ERPNextItemToSync(
				item=found_item,
				item_woocommerce_server_idx=next(
					server.idx for server in found_item.woocommerce_servers if server.name == item_codes[0].name
				),
			)

	def sync_wc_product_with_erpnext_item(self):
		"""
		Synchronise Item between ERPNext and WooCommerce.
		Supports force_push to immediately push ERPNext changes to WooCommerce.
		"""
		if self.item and not self.woocommerce_product:
			# create missing product in WooCommerce
			self.create_woocommerce_product(self.item)
		elif self.woocommerce_product and not self.item:
			# create missing item in ERPNext
			self.create_item(self.woocommerce_product)
		elif self.item and self.woocommerce_product:
			if getattr(self, "force_push", False):
				self.update_woocommerce_product(self.woocommerce_product, self.item)
			else:
				# both exist, check sync hash
				if (
					self.woocommerce_product.woocommerce_date_modified
					!= self.item.item_woocommerce_server.woocommerce_last_sync_hash
				):
					if get_datetime(self.woocommerce_product.woocommerce_date_modified) > get_datetime(
						self.item.item.modified
					):
						self.update_item(self.woocommerce_product, self.item)
					elif get_datetime(self.woocommerce_product.woocommerce_date_modified) < get_datetime(
						self.item.item.modified
					):
						self.update_woocommerce_product(self.woocommerce_product, self.item)

	def update_item(self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync):
		"""
		Update the ERPNext Item with fields from its corresponding WooCommerce Product
		"""
		item_dirty = False
		if item.item.item_name != woocommerce_product.woocommerce_name:
			item.item.item_name = woocommerce_product.woocommerce_name
			item_dirty = True

		fields_updated, item.item = self.set_item_fields(item=item.item)

		wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
		if wc_server.enable_image_sync:
			wc_product_images = json.loads(woocommerce_product.images) if woocommerce_product.images else []
			if len(wc_product_images) > 0:
				if item.item.image != wc_product_images[0].get("src"):
					item.item.image = wc_product_images[0].get("src")
					item_dirty = True

		if item_dirty or fields_updated:
			item.item.flags.created_by_sync = True
			item.item.flags.in_sync = True
			item.item.flags.ignore_mandatory = True
			try:
				item.item.save()
			except frappe.exceptions.TimestampMismatchError:
				item.item.reload()
				fields_updated, item.item = self.set_item_fields(item=item.item)
				item.item.flags.created_by_sync = True
				item.item.flags.in_sync = True
				item.item.flags.ignore_mandatory = True
				item.item.save()

		self.set_sync_hash()

	def update_woocommerce_product(
		self, wc_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> None:
		"""
		Update the WooCommerce Product with fields from its corresponding ERPNext Item
		"""
		wc_product_dirty = False

		# Skip or draft simple items and variations with zero or no price
		if not item.item.has_variants:
			pricing = get_item_price_rate(item)
			if pricing is None or float(pricing) <= 0:
				if wc_product.status != "draft":
					wc_product.status = "draft"
					wc_product.flags.ignore_version = True
					wc_product.save()
					self.woocommerce_product = wc_product
					self.set_sync_hash()
				frappe.logger("woocommerce_fusion").info(
					f"Item {item.item.name} has zero or no price. Marked WooCommerce product {wc_product.woocommerce_id} as draft."
				)
				return
			else:
				# If product was previously draft due to zero price, restore publish status
				if wc_product.status == "draft":
					wc_server = frappe.get_cached_doc(
						"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
					)
					wc_product.status = wc_server.new_product_publish_status or "publish"
					wc_product_dirty = True

				# Update regular price if price sync is enabled and pricing has changed
				wc_server = frappe.get_cached_doc(
					"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
				)
				if wc_server.enable_price_list_sync and pricing is not None:
					try:
						current_price = float(wc_product.regular_price or 0)
						new_price = float(pricing)
						if current_price != new_price:
							wc_product.regular_price = str(pricing)
							wc_product_dirty = True
					except (ValueError, TypeError):
						if str(wc_product.regular_price) != str(pricing):
							wc_product.regular_price = str(pricing)
							wc_product_dirty = True

		# Update properties
		if wc_product.woocommerce_name != item.item.item_name:
			wc_product.woocommerce_name = item.item.item_name
			wc_product_dirty = True

		if item.item.item_code and getattr(wc_product, "sku", None) != item.item.item_code:
			wc_product.sku = item.item.item_code
			wc_product_dirty = True

		product_fields_changed, wc_product = self.set_product_fields(wc_product, item)
		if product_fields_changed:
			wc_product_dirty = True

		# Image upload: ERPNext -> WooCommerce
		image_dirty = self._sync_item_image_to_woocommerce(wc_product, item)
		if image_dirty:
			wc_product_dirty = True

		if wc_product_dirty or getattr(self, "force_push", False):
			wc_product.flags.ignore_version = True
			wc_product.flags.force_push = getattr(self, "force_push", False)
			wc_product.save()

		self.woocommerce_product = wc_product
		self.set_sync_hash()

	def create_woocommerce_product(self, item: ERPNextItemToSync) -> None:
		"""
		Create the WooCommerce Product with fields from its corresponding ERPNext Item
		"""
		if (
			item.item_woocommerce_server.woocommerce_server
			and item.item_woocommerce_server.enabled
			and not item.item_woocommerce_server.woocommerce_id
		):
			# Skip simple items and variations that have zero or no price
			if not item.item.has_variants:
				pricing = get_item_price_rate(item)
				if pricing is None or float(pricing) <= 0:
					frappe.logger("woocommerce_fusion").info(
						f"Skipping WooCommerce sync for Item {item.item.name}: price is zero or not set."
					)
					return
			# Create a new WooCommerce Product doc
			wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})

			wc_product.type = "simple"

			# Handle variants
			if item.item.has_variants:
				wc_product.type = "variable"
				wc_product_attributes = []

				# Handle attributes
				for row in item.item.attributes:
					item_attribute = frappe.get_doc("Item Attribute", row.attribute)
					wc_product_attributes.append(
						{
							"name": row.attribute,
							"slug": row.attribute.lower().replace(" ", "_"),
							"visible": True,
							"variation": True,
							"options": [option.attribute_value for option in item_attribute.item_attribute_values],
						}
					)

				wc_product.attributes = json.dumps(wc_product_attributes)

			if item.item.variant_of:
				# Check if parent exists
				parent_item = frappe.get_doc("Item", item.item.variant_of)
				parent_item, parent_wc_product = run_item_sync(item_code=parent_item.item_code)
				wc_product.parent_id = parent_wc_product.woocommerce_id
				wc_product.type = "variation"

				# Handle attributes
				wc_product_attributes = [
					{
						"name": row.attribute,
						"slug": row.attribute.lower().replace(" ", "_"),
						"option": row.attribute_value,
					}
					for row in item.item.attributes
				]

				wc_product.attributes = json.dumps(wc_product_attributes)

			# Set properties
			wc_product.woocommerce_server = item.item_woocommerce_server.woocommerce_server
			wc_product.woocommerce_name = item.item.item_name
			wc_product.regular_price = get_item_price_rate(item) or "0"
			wc_product.sku = item.item.item_code

			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
			)
			wc_product.status = wc_server.new_product_publish_status or "draft"

			self.set_product_fields(wc_product, item)

			wc_product.insert()
			if not wc_product.name or WC_RESOURCE_DELIMITER not in str(wc_product.name):
				wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
					domain=item.item_woocommerce_server.woocommerce_server,
					resource_id=wc_product.woocommerce_id,
				)
			self.woocommerce_product = wc_product

			# Reload ERPNext Item and guarantee woocommerce_id persistence
			frappe.db.set_value(
				"Item WooCommerce Server",
				item.item_woocommerce_server.name,
				"woocommerce_id",
				str(wc_product.woocommerce_id),
				update_modified=False,
			)
			item.item.reload()
			item.item_woocommerce_server.woocommerce_id = str(wc_product.woocommerce_id)
			item.item.flags.created_by_sync = True
			item.item.flags.in_sync = True
			try:
				item.item.save()
			except frappe.exceptions.TimestampMismatchError:
				item.item.reload()
				item.item_woocommerce_server.woocommerce_id = str(wc_product.woocommerce_id)
				item.item.flags.created_by_sync = True
				item.item.flags.in_sync = True
				item.item.save()

			# Upload image to WooCommerce after product is created (woocommerce_id is now available)
			if self._sync_item_image_to_woocommerce(wc_product, item):
				wc_product.save()

			self.set_sync_hash()

	def create_item(self, wc_product: WooCommerceProduct) -> None:
		"""
		Create an ERPNext Item from the given WooCommerce Product
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

		# Create Item
		item = frappe.new_doc("Item")

		# Handle variants' attributes
		if wc_product.type in ["variable", "variation"]:
			self.create_or_update_item_attributes(wc_product)
			wc_attributes = json.loads(wc_product.attributes)
			for wc_attribute in wc_attributes:
				row = item.append("attributes")
				row.attribute = wc_attribute["name"]
				if wc_product.type == "variation":
					row.attribute_value = wc_attribute["option"]

		# Handle variants
		if wc_product.type == "variable":
			item.has_variants = 1

		if wc_product.type == "variation":
			# Check if parent exists
			woocommerce_product_name = generate_woocommerce_record_name_from_domain_and_id(
				wc_product.woocommerce_server, wc_product.parent_id
			)
			parent_item, parent_wc_product = run_item_sync(
				woocommerce_product_name=woocommerce_product_name, enqueue=False
			)
			if parent_item:
				item.variant_of = parent_item.item_code

		item.item_code = (
			wc_product.sku
			if wc_server.name_by == "Product SKU" and wc_product.sku
			else str(wc_product.woocommerce_id)
		)
		item.stock_uom = wc_server.uom or _("Nos")
		item.item_group = wc_server.item_group
		item.item_name = wc_product.woocommerce_name
		row = item.append("woocommerce_servers")
		row.woocommerce_id = wc_product.woocommerce_id
		row.woocommerce_server = wc_server.name
		item.flags.ignore_mandatory = True
		item.flags.created_by_sync = True
		item.flags.in_sync = True

		if wc_server.enable_image_sync:
			wc_product_images = json.loads(wc_product.images) if wc_product.images else []
			if len(wc_product_images) > 0:
				item.image = wc_product_images[0]["src"]

		modified, item = self.set_item_fields(item=item)
		item.flags.created_by_sync = True
		item.flags.in_sync = True

		item.insert()

		self.item = ERPNextItemToSync(
			item=item,
			item_woocommerce_server_idx=next(
				iws.idx
				for iws in item.woocommerce_servers
				if iws.woocommerce_server == wc_product.woocommerce_server
			),
		)

		self.set_sync_hash()

	def create_or_update_item_attributes(self, wc_product: WooCommerceProduct):
		"""
		Create or update an Item Attribute.
		Appends missing attribute values to the master without removing existing ones,
		preventing InvalidItemAttributeValueError when values are used by other items.
		"""
		if not wc_product.attributes:
			return

		wc_attributes = (
			json.loads(wc_product.attributes)
			if isinstance(wc_product.attributes, str)
			else wc_product.attributes
		)
		for wc_attribute in wc_attributes:
			attr_name = wc_attribute.get("name")
			if not attr_name:
				continue

			if frappe.db.exists("Item Attribute", attr_name):
				item_attribute = frappe.get_doc("Item Attribute", attr_name)
			else:
				item_attribute = frappe.get_doc(
					{"doctype": "Item Attribute", "attribute_name": attr_name}
				)

			# Get list of attribute options
			if wc_product.type == "variable":
				options = wc_attribute.get("options", [])
			else:
				opt = wc_attribute.get("option")
				options = [opt] if opt else []

			existing_values = {val.attribute_value for val in item_attribute.item_attribute_values}
			dirty = False
			for option in options:
				if option and option not in existing_values:
					row = item_attribute.append("item_attribute_values")
					row.attribute_value = option
					row.abbr = str(option).replace(" ", "")
					existing_values.add(option)
					dirty = True

			item_attribute.flags.ignore_mandatory = True
			if not frappe.db.exists("Item Attribute", attr_name):
				item_attribute.insert()
			elif dirty:
				item_attribute.save()

	def set_item_fields(self, item: Item) -> Tuple[bool, Item]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		WooCommerce to ERPNext
		"""
		item_dirty = False
		if item and self.woocommerce_product:
			wc_server = frappe.get_cached_doc(
				"WooCommerce Server", self.woocommerce_product.woocommerce_server
			)
			if wc_server.item_field_map:
				woocommerce_product_dict = (
					self.woocommerce_product.deserialize_attributes_of_type_dict_or_list(
						self.woocommerce_product.to_dict()
					)
				)
				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")

					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(woocommerce_product_dict)

					if woocommerce_product_field_matches:
						setattr(item, erpnext_item_field_name[0], woocommerce_product_field_matches[0].value)
						item_dirty = True
		return item_dirty, item

	def set_product_fields(
		self, woocommerce_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> Tuple[bool, WooCommerceProduct]:
		"""
		If there exist any Field Mappings on `WooCommerce Server`, attempt to synchronise their values from
		ERPNext to WooCommerce

		Returns true if woocommerce_product was changed
		"""
		wc_product_dirty = False
		if item and woocommerce_product:
			wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_product.woocommerce_server)
			if wc_server.item_field_map:
				wc_dict = woocommerce_product.to_dict()
				wc_product_with_deserialised_fields = (
					woocommerce_product.deserialize_attributes_of_type_dict_or_list(wc_dict)
				)

				for map in wc_server.item_field_map:
					erpnext_item_field_name = map.erpnext_field_name.split(" | ")
					erpnext_item_field_value = getattr(item.item, erpnext_item_field_name[0])

					jsonpath_expr = parse(map.woocommerce_field_name)
					woocommerce_product_field_matches = jsonpath_expr.find(wc_product_with_deserialised_fields)

					if (
						not woocommerce_product_field_matches
						or woocommerce_product_field_matches[0].value != erpnext_item_field_value
					):
						jsonpath_expr.update_or_create(
							wc_product_with_deserialised_fields, erpnext_item_field_value
						)
						wc_product_dirty = True

				if wc_product_dirty:
					serialized_dict = woocommerce_product.serialize_attributes_of_type_dict_or_list(
						wc_product_with_deserialised_fields
					)
					for k, v in serialized_dict.items():
						woocommerce_product.set(k, v)

		woocommerce_product.serialize_attributes_of_type_dict_or_list(woocommerce_product)
		return wc_product_dirty, woocommerce_product

	def _sync_item_image_to_woocommerce(
		self, wc_product: WooCommerceProduct, item: ERPNextItemToSync
	) -> bool:
		"""
		Upload or update the ERPNext Item image to WooCommerce via the woo-media-api plugin.
		Returns True if the WooCommerce product's images field was updated.
		"""
		wc_server = frappe.get_cached_doc("WooCommerce Server", wc_product.woocommerce_server)

		if not wc_server.enable_erpnext_to_wc_image_upload:
			return False

		if not item.item.image:
			return False

		image_details = frappe.db.get_value(
			"File",
			{"file_url": item.item.image},
			["file_name", "file_url", "is_private", "content_hash", "modified"],
		)
		if not image_details:
			return False

		file_name, file_url, is_private, content_hash, modified = image_details
		image_url = format_erpnext_img_url(image_details)
		if not image_url:
			return False

		current_image_id = item.item_woocommerce_server.get("woocommerce_image_id") or None
		last_image_url = item.item_woocommerce_server.get("woocommerce_last_image_url") or None
		last_image_hash = item.item_woocommerce_server.get("woocommerce_image_hash") or None

		# 1. Skip upload if image has already been synced and content has not changed
		if current_image_id:
			image_unchanged = False
			if last_image_hash and content_hash and str(last_image_hash) == str(content_hash):
				image_unchanged = True
			elif not last_image_hash and last_image_url and last_image_url == image_url:
				image_unchanged = True

			if image_unchanged:
				# Ensure that the existing image is actually assigned to the WooCommerce product.
				# If WooCommerce product has no image or lost its featured image, re-assign current_image_id.
				current_int_id = safe_int(current_image_id)
				wc_images = getattr(wc_product, "images", None)
				if isinstance(wc_images, str) and wc_images:
					try:
						wc_images = json.loads(wc_images)
					except Exception:
						wc_images = []

				has_image_assigned = False
				if isinstance(wc_images, list):
					for img in wc_images:
						if isinstance(img, dict) and safe_int(img.get("id")) == current_int_id:
							has_image_assigned = True
							break

				if has_image_assigned:
					return False

				if isinstance(wc_images, list) and not has_image_assigned:
					wc_product.images = json.dumps([{"id": current_int_id}])
					return True

				return False

		# 2. Attachment Discovery: If ERPNext has no image_id recorded yet, check if
		# WooCommerce product already has an image attached with matching name or file
		if not current_image_id and wc_product.images:
			existing_wc_images = json.loads(wc_product.images) if isinstance(wc_product.images, str) else wc_product.images
			if existing_wc_images and isinstance(existing_wc_images, list) and len(existing_wc_images) > 0:
				first_img = existing_wc_images[0]
				first_img_id = safe_int(first_img.get("id")) if isinstance(first_img, dict) else None
				if first_img_id:
					wc_img_name = str(first_img.get("name", "")).lower()
					wc_img_src = str(first_img.get("src", "")).lower()
					clean_file_base = file_name.rsplit(".", 1)[0].lower()
					if clean_file_base in wc_img_name or clean_file_base in wc_img_src:
						# Same image already on WooCommerce: adopt existing ID
						existing_id = first_img_id
						self._update_item_wc_image_meta(
							item.item_woocommerce_server.name,
							existing_id,
							image_url,
							content_hash,
						)
						return False
					else:
						# Different image: mark existing ID as old_image_id for safe cleanup
						current_image_id = str(first_img["id"])

		# 3. Upload or update image via woo-media-api with content hash
		media_response = self.handle_media_update(
			wc_server=wc_server,
			wc_product=wc_product,
			image_url=image_url,
			title=file_name,
			alt_text=item.item.item_name,
			old_image_id=current_image_id,
			content_hash=content_hash,
		)

		if not media_response or not media_response.get("id"):
			return False

		new_image_id = safe_int(media_response.get("id"))
		wc_product.images = json.dumps(
			[
				{
					"id": new_image_id,
					"src": media_response.get("src", ""),
					"name": media_response.get("name", file_name),
					"alt": media_response.get("alt", item.item.item_name),
				}
			]
		)

		self._update_item_wc_image_meta(
			item.item_woocommerce_server.name,
			new_image_id,
			image_url,
			content_hash,
		)

		return True

	def _update_item_wc_image_meta(
		self, item_wc_server_name: str, image_id: int, image_url: str, content_hash: Optional[str]
	):
		update_values = {
			"woocommerce_image_id": str(image_id),
			"woocommerce_last_image_url": image_url,
		}
		columns = frappe.db.get_table_columns("Item WooCommerce Server")
		if "woocommerce_image_hash" in columns and content_hash:
			update_values["woocommerce_image_hash"] = content_hash

		frappe.db.set_value(
			"Item WooCommerce Server",
			item_wc_server_name,
			update_values,
			update_modified=False,
		)

	def handle_media_update(
		self,
		wc_server,
		wc_product: WooCommerceProduct,
		image_url: str,
		title: str,
		alt_text: str,
		old_image_id: Optional[str] = None,
		content_hash: Optional[str] = None,
	) -> Optional[dict]:
		"""
		Upload a new image to the WooCommerce Media Library via the woo-media-api plugin,
		safely requesting cleanup of the old image if replaced.
		"""
		wc_api = APIWithRequestLogging(
			url=wc_server.woocommerce_server_url,
			consumer_key=wc_server.api_consumer_key,
			consumer_secret=wc_server.api_consumer_secret,
			version="wc/v3",
			timeout=40,
		)

		media_data = {
			"image_url": image_url,
			"title": title,
			"alt_text": alt_text,
			"post": wc_product.woocommerce_id,
		}
		if content_hash:
			media_data["content_hash"] = content_hash

		try:
			response = wc_api.post("media", data=media_data)
			response.raise_for_status()
			media_response = response.json()

			new_id = str(media_response.get("ID") or media_response.get("id"))

			# Only delete old_image_id if it differs from the new ID
			if old_image_id and str(old_image_id) != str(new_id):
				try:
					wc_api.delete(f"media/{old_image_id}")
				except Exception:
					frappe.log_error(
						f"WooCommerce Media: cleanup request for old media ID {old_image_id}",
						title="WooCommerce Media Cleanup",
					)

			return {
				"id": new_id,
				"src": media_response.get("guid", image_url),
				"name": media_response.get("post_title", title),
				"alt": media_response.get("post_excerpt") or alt_text,
			}

		except Exception:
			error_message = f"{frappe.get_traceback()}\n\nMedia upload data:\n{str(media_data)}"
			frappe.log_error("WooCommerce Media Upload Error", error_message)
			return None

	def set_sync_hash(self):
		"""
		Set the last sync hash value using db.set_value without ORM triggers
		"""
		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"woocommerce_last_sync_hash",
			self.woocommerce_product.woocommerce_date_modified,
			update_modified=False,
		)

		frappe.db.set_value(
			"Item WooCommerce Server",
			self.item.item_woocommerce_server.name,
			"enabled",
			1,
			update_modified=False,
		)


def get_list_of_wc_products(
	item: Optional[ERPNextItemToSync] = None, date_time_from: Optional[datetime] = None
) -> List[WooCommerceProduct]:
	"""
	Fetches a list of WooCommerce Products within a specified date range or linked with an Item.
	"""
	if not any([date_time_from, item]):
		raise ValueError("At least one of date_time_from or item parameters are required")

	wc_records_per_page_limit = 100
	page_length = wc_records_per_page_limit
	new_results = True
	start = 0
	filters = []
	wc_products = []
	servers = None

	if date_time_from:
		filters.append(["WooCommerce Product", "date_modified", ">", date_time_from])
	if item:
		filters.append(["WooCommerce Product", "id", "=", item.item_woocommerce_server.woocommerce_id])
		servers = [item.item_woocommerce_server.woocommerce_server]

	while new_results:
		woocommerce_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		new_results = woocommerce_product.get_list(
			args={
				"filters": filters,
				"page_length": page_length,
				"start": start,
				"servers": servers,
				"as_doc": True,
			}
		)
		for wc_product in new_results:
			wc_products.append(wc_product)
		start += page_length
		if len(new_results) < page_length:
			new_results = []

	return wc_products


def get_item_price_rate(item: ERPNextItemToSync):
	"""
	Get the Item Price if Item Price sync is enabled
	"""
	wc_server = frappe.get_cached_doc(
		"WooCommerce Server", item.item_woocommerce_server.woocommerce_server
	)
	if wc_server.enable_price_list_sync and wc_server.price_list:
		item_code = getattr(item.item, "item_code", None) or item.item.name
		item_prices = frappe.get_all(
			"Item Price",
			filters={"item_code": item_code, "price_list": wc_server.price_list},
			fields=["price_list_rate", "valid_upto"],
		)
		if not item_prices and getattr(item.item, "item_name", None) and item.item.item_name != item_code:
			item_prices = frappe.get_all(
				"Item Price",
				filters={"item_code": item.item.item_name, "price_list": wc_server.price_list},
				fields=["price_list_rate", "valid_upto"],
			)
		return next(
			(
				price.price_list_rate
				for price in item_prices
				if (not price.valid_upto or price.valid_upto > now()) and price.price_list_rate is not None
			),
			None,
		)


def clear_sync_hash_and_run_item_sync(item_code: str):
	"""
	Clear the last sync hash value and trigger a targeted push to WooCommerce
	"""
	iws = frappe.qb.DocType("Item WooCommerce Server")

	iwss = (
		frappe.qb.from_(iws).where(iws.enabled == 1).where(iws.parent == item_code).select(iws.name)
	).run(as_dict=True)

	for row in iwss:
		frappe.db.set_value(
			"Item WooCommerce Server",
			row.name,
			"woocommerce_last_sync_hash",
			None,
			update_modified=False,
		)

	if len(iwss) > 0:
		run_item_sync(item_code=item_code, force_push=True, enqueue=False)
