import unittest
from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe import _dict

from woocommerce_fusion.tasks.stock_update import (
	update_stock_levels_for_all_enabled_items_in_background,
	update_stock_levels_on_woocommerce_site,
)


class TestWooCommerceStockSync(unittest.TestCase):

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site(self, mock_wc_api, mock_frappe):
		# Set up a dummy item set to sync to two different WC sites
		some_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=1, woocommerce_server="woo1.example.com", enabled=1),
				frappe._dict(woocommerce_id=2, woocommerce_server="woo2.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
		)

		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(item_code="some_item_code", warehouse="Warehouse A", actual_qty=5),
			frappe._dict(item_code="some_item_code", warehouse="Warehouse B", actual_qty=10),
			frappe._dict(item_code="some_item_code", warehouse="Warehouse C", actual_qty=20),
		]

		# Mock get_all returning different values depending on Doctype
		def mock_get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "WooCommerce Server":
				return [frappe._dict(name="woo1.example.com"), frappe._dict(name="woo2.example.com")]
			elif doctype == "Bin":
				return bin_list
			elif doctype == "WooCommerce Product":
				return []
			elif doctype == "Item WooCommerce Server":
				return []
			return []
		mock_frappe.get_all.side_effect = mock_get_all_side_effect

		# Mock get_doc to return either the Item or the WooCommerce Server config doc
		def mock_get_doc_side_effect(doctype, name=None):
			if doctype == "Item":
				return some_item
			elif doctype == "WooCommerce Server":
				if name == "woo1.example.com":
					return frappe._dict(
						name="woo1.example.com",
						woocommerce_server="woo1.example.com",
						woocommerce_server_url="https://woo1.example.com",
						enable_sync=1,
						enable_stock_level_synchronisation=1,
						subtract_reserved_stock=0,
						warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
					)
				elif name == "woo2.example.com":
					return frappe._dict(
						name="woo2.example.com",
						woocommerce_server="woo2.example.com",
						woocommerce_server_url="https://woo2.example.com",
						enable_sync=1,
						enable_stock_level_synchronisation=1,
						subtract_reserved_stock=0,
						warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
					)
			return frappe._dict()
		mock_frappe.get_doc.side_effect = mock_get_doc_side_effect

		# Mock SQL results for items to sync
		mock_frappe.db.sql.side_effect = [
			[frappe._dict(item_code="some_item_code", woocommerce_id="1", enabled=1, variant_of=None, is_stock_item=1, disabled=0)],
			[frappe._dict(item_code="some_item_code", woocommerce_id="2", enabled=1, variant_of=None, is_stock_item=1, disabled=0)]
		]

		# Mock out calls to WooCommerce API's
		mock_post_response = Mock()
		mock_post_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.post.return_value = mock_post_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("some_item_code")

		# Assert that the inventories post calls were made with the correct arguments
		self.assertEqual(mock_api_instance.post.call_count, 2)
		actual_post_endpoints = [call.kwargs["endpoint"] for call in mock_api_instance.post.call_args_list]
		actual_post_data = [call.kwargs["data"] for call in mock_api_instance.post.call_args_list]

		expected_post_endpoints = ["products/batch", "products/batch"]
		expected_post_data = [
			{"update": [{"id": "1", "stock_quantity": 15}]},
			{"update": [{"id": "2", "stock_quantity": 15}]}
		]
		self.assertEqual(actual_post_endpoints, expected_post_endpoints)
		self.assertEqual(actual_post_data, expected_post_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	@patch("woocommerce_fusion.tasks.stock_update.APIWithRequestLogging", autospec=True)
	def test_update_stock_levels_on_woocommerce_site_variant(self, mock_wc_api, mock_frappe):
		# Set up a dummy variant item set to sync to a WC site
		variant_item = frappe._dict(
			woocommerce_servers=[
				frappe._dict(woocommerce_id=101, woocommerce_server="woo1.example.com", enabled=1),
			],
			is_stock_item=1,
			disabled=0,
			variant_of="parent_item_code",
		)
		
		# Set up a dummy bin list with stock in two Warehouses
		bin_list = [
			frappe._dict(item_code="variant_item_code", warehouse="Warehouse A", actual_qty=5),
			frappe._dict(item_code="variant_item_code", warehouse="Warehouse B", actual_qty=10),
		]

		# Mock get_all
		def mock_get_all_side_effect(doctype, *args, **kwargs):
			if doctype == "WooCommerce Server":
				return [frappe._dict(name="woo1.example.com")]
			elif doctype == "Bin":
				return bin_list
			elif doctype == "WooCommerce Product":
				return [frappe._dict(woocommerce_id="101", parent_id="100")]
			elif doctype == "Item WooCommerce Server":
				return []
			return []
		mock_frappe.get_all.side_effect = mock_get_all_side_effect

		def mock_get_doc_side_effect(doctype, name=None):
			if doctype == "Item":
				return variant_item
			elif doctype == "WooCommerce Server":
				return frappe._dict(
					name="woo1.example.com",
					woocommerce_server="woo1.example.com",
					woocommerce_server_url="https://woo1.example.com",
					enable_sync=1,
					enable_stock_level_synchronisation=1,
					subtract_reserved_stock=0,
					warehouses=[frappe._dict(warehouse="Warehouse A"), frappe._dict(warehouse="Warehouse B")],
				)
			return frappe._dict()
		mock_frappe.get_doc.side_effect = mock_get_doc_side_effect

		# Mock SQL results
		mock_frappe.db.sql.return_value = [
			frappe._dict(item_code="variant_item_code", woocommerce_id="101", enabled=1, variant_of="parent_item_code", is_stock_item=1, disabled=0)
		]

		# Mock out calls to WooCommerce API's
		mock_post_response = Mock()
		mock_post_response.status_code = 200

		mock_api_instance = MagicMock()
		mock_api_instance.post.return_value = mock_post_response
		mock_wc_api.return_value = mock_api_instance

		# Call function under test
		update_stock_levels_on_woocommerce_site("variant_item_code")

		# Assert that the inventories post calls were made with the correct arguments
		self.assertEqual(mock_api_instance.post.call_count, 1)
		actual_post_endpoint = mock_api_instance.post.call_args.kwargs["endpoint"]
		actual_post_data = mock_api_instance.post.call_args.kwargs["data"]

		expected_post_endpoint = "products/100/variations/batch"
		expected_data = {"update": [{"id": "101", "stock_quantity": 15}]}
		self.assertEqual(actual_post_endpoint, expected_post_endpoint)
		self.assertEqual(actual_post_data, expected_data)

	@patch("woocommerce_fusion.tasks.stock_update.frappe")
	def test_update_stock_levels_for_all_enabled_items_in_background(self, mock_frappe):
		# Set up mock return values
		mock_frappe.db.get_all.side_effect = [
			[_dict({"name": f"Item-1-{x}"}) for x in range(500)],  # First page of results
			[_dict({"name": f"Item-2-{x}"}) for x in range(500)],  # Second page of results
			[],  # No more results, loop should exit
		]

		# Call the function
		update_stock_levels_for_all_enabled_items_in_background()

		# Assertions to check if get_all was called correctly
		self.assertEqual(mock_frappe.db.get_all.call_count, 3)
		expected_calls = [
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=0, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=500, page_length=500),
			call(doctype="Item", filters={"disabled": 0}, fields=["name"], start=1000, page_length=500),
		]
		mock_frappe.db.get_all.assert_has_calls(expected_calls, any_order=True)

		# Assertions to check if enqueue was called correctly
		self.assertEqual(mock_frappe.enqueue.call_count, 1)
		enqueued_func = mock_frappe.enqueue.call_args.args[0]
		enqueued_kwargs = mock_frappe.enqueue.call_args.kwargs
		self.assertEqual(enqueued_func, "woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site_multiple")
		self.assertEqual(len(enqueued_kwargs["item_codes"]), 1000)
