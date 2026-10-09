# Copyright (c) 2024, Dirk van der Laarse and contributors
# For license information, please see license.txt

import json
from dataclasses import dataclass
from typing import Dict

from woocommerce_fusion.woocommerce.woocommerce_api import WooCommerceAPI, WooCommerceResource


@dataclass
class WooCommerceProductAPI(WooCommerceAPI):
	"""Class for keeping track of a WooCommerce site."""

	pass


class WooCommerceProduct(WooCommerceResource):
	"""
	Virtual doctype for WooCommerce Products
	"""

	doctype = "WooCommerce Product"
	resource: str = "products"
	child_resource: str = "variations"
	field_setter_map = {"woocommerce_name": "name", "woocommerce_id": "id"}

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_list(args):
		products = WooCommerceProduct.get_list_of_records(args)

		# Extend the list with product variants
		products_with_variants = [
			(product.get("id"), product.get("woocommerce_name"))
			for product in products
			if product.get("type") == "variable"
		]
		for id, woocommerce_name in products_with_variants:
			args["endpoint"] = f"products/{id}/variations"
			args["metadata"] = {"parent_woocommerce_name": woocommerce_name}
			variants = WooCommerceProduct.get_list_of_records(args)
			products.extend(variants)

		return products

	def after_load_from_db(self, product: Dict):
		product.pop("name")
		product = self.set_title(product)
		return product

	@classmethod
	def during_get_list_of_records(cls, product: Dict, args):
		# In the case of variations
		if product["parent_id"]:
			# Woocommerce product variantions endpoint results doesn't return the type, so set it manually
			product["type"] = "variation"

			if variation_name := cls.get_variation_name(product, args):
				# Set the name in args, for use by set_title()
				args["metadata"]["woocommerce_name"] = variation_name

				# Override the woocommerce_name field
				product = cls.override_woocommerce_name(product, variation_name)

		product = cls.set_title(product, args)
		return product

	@staticmethod
	def set_title(product: dict, args=None):
		if (
			args and (metadata := args.get("metadata")) and (set_name := metadata.get("woocommerce_name"))
		):
			product["title"] = set_name
		elif wc_name := product.get("woocommerce_name"):
			if sku := product.get("sku"):
				product["title"] = f"{sku} - {wc_name}"
			else:
				product["title"] = wc_name
		else:
			product["title"] = product["woocommerce_id"]

		return product

	@staticmethod
	def override_woocommerce_name(product: Dict, name: str):
		product["woocommerce_name"] = name
		return product

	@staticmethod
	def get_variation_name(product: Dict, args):
		# If this is a variation, we expect the variation's parent name in the metadata, then we can
		# build an item name in the format of {parent_name}, {attribute 1}, {attribute n}
		if (
			(product["type"] == "variation")
			and (metadata := args.get("metadata"))
			and (attributes := product.get("attributes"))
			and (parent_wc_name := metadata.get("parent_woocommerce_name"))
		):
			attr_values = [attr["option"] for attr in json.loads(attributes)]
			return parent_wc_name + " - " + ", ".join(attr_values)
		return None

	# use "args" despite frappe-semgrep-rules.rules.overusing-args, following convention in ERPNext
	# nosemgrep
	@staticmethod
	def get_count(args) -> int:
		return WooCommerceProduct.get_count_of_records(args)

	def before_db_insert(self, product: Dict):
		return self.clean_up_product_before_write(product)

	def before_db_update(self, product: Dict):
		product = self.clean_up_product_before_write(product)

		# Prevent image duplication on PUT requests:
		# When images already have a WordPress Attachment ID, send only {"id": <int>}
		# instead of the full object with "src" URL. WooCommerce re-downloads images
		# from URLs on every PUT, creating duplicates. Sending just the ID makes
		# WooCommerce link to the existing media entry.
		if "images" in product and product["images"]:
			images = product["images"]
			if isinstance(images, str):
				images = json.loads(images)

			id_only_images = []
			for img in images:
				if isinstance(img, dict) and img.get("id"):
					# Send only the attachment ID — WooCommerce will keep the
					# existing media file instead of downloading a new copy
					try:
						id_only_images.append({"id": int(img["id"])})
					except (ValueError, TypeError):
						# If ID is not a valid integer, keep the full image dict
						id_only_images.append(img)
				else:
					# No ID available: omit raw src during PUT updates to prevent
					# WooCommerce from auto-downloading duplicates
					pass

			product["images"] = id_only_images

		return product

	def after_db_update(self):
		pass

	@staticmethod
	def clean_up_product_before_write(product):
		"""
		Perform some tasks to make sure that a product is in the correct format for the WC API
		"""
		# Convert back to string
		if product.get("weight") is not None:
			product["weight"] = str(product["weight"])

		# Do not post regular_price if product is variable or price is empty/None
		if product.get("type") == "variable" or product.get("regular_price") in (None, "", "None"):
			product.pop("regular_price", None)
		else:
			product["regular_price"] = str(product["regular_price"])

		# Do not post Sale Price if it is 0
		if product.get("sale_price") and float(product.get("sale_price", 0)) > 0:
			product["sale_price"] = str(product["sale_price"])
		else:
			product.pop("sale_price", None)

		# Set corrected properties
		if product.get("woocommerce_name") is not None:
			product["name"] = str(product["woocommerce_name"])

		# Ensure JSON fields are parsed into native Python objects (dicts/lists), not JSON strings
		for json_key in ("dimensions", "upsell_ids", "cross_sell_ids", "categories", "tags", "attributes", "default_attributes", "meta_data"):
			val = product.get(json_key)
			if isinstance(val, str):
				try:
					product[json_key] = json.loads(val)
				except Exception:
					product.pop(json_key, None)

		# Drop Frappe-specific UI / read-only fields
		fields_to_drop = [
			"related_ids", "title", "permalink", "price",
			"woocommerce_server", "woocommerce_id", "woocommerce_name",
			"woocommerce_date_created", "woocommerce_date_created_gmt",
			"woocommerce_date_modified", "woocommerce_date_modified_gmt",
			"rating_count", "average_rating", "total_sales",
			"date_on_sale_from", "date_on_sale_from_gmt", "date_on_sale_to", "date_on_sale_to_gmt",
		]
		for key in list(product.keys()):
			if key in fields_to_drop or key.startswith("section_break_") or key.startswith("column_break_") or key.endswith("_tab"):
				product.pop(key, None)

		return product
