from unittest.mock import MagicMock, Mock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.sync_items import ERPNextItemToSync, SynchroniseItem
from woocommerce_fusion.woocommerce.woocommerce_api import (
	generate_woocommerce_record_name_from_domain_and_id,
)


@patch("woocommerce_fusion.tasks.sync_items.run_item_sync")
@patch.object(SynchroniseItem, "set_sync_hash")
class TestWooCommerceSync(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		# important to call super() methods when extending TestCase.
		super().setUpClass()

	@patch.object(SynchroniseItem, "update_item")
	def test_sync_items_while_passing_item_should_update_item_if_item_is_older(
		self, mock_update_item, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should update the item
		if the item is older than the corresponding WooCommerce product
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Item
		item = frappe.get_doc({"doctype": "Item"})
		item.name = "ITEM-0001"
		row = item.append("woocommerce_servers")
		row.woocommerce_id = woocommerce_id
		row.woocommerce_server = woocommerce_server
		item.modified = "2023-01-01"
		sync.item = ERPNextItemToSync(item, 1)

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_product.woocommerce_date_modified = "2023-12-31"
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be updated
		mock_update_item.assert_called_once_with(wc_product, sync.item)

	@patch("woocommerce_fusion.tasks.sync_items.frappe")
	@patch.object(SynchroniseItem, "update_woocommerce_product")
	def test_sync_items_while_passing_item_should_update_wc_product_if_item_is_newer(
		self, mock_update_woocommerce_product, mock_frappe, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should update the WooCommerce product
		if the item is newer than the corresponding WooCommerce product
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		mock_frappe.get_doc.return_value = Mock()
		mock_frappe.get_single.return_value = Mock()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy Item
		item = frappe.get_doc({"doctype": "Item"})
		item.name = "ITEM-0001"
		row = item.append("woocommerce_servers")
		row.woocommerce_id = woocommerce_id
		row.woocommerce_server = woocommerce_server
		item.modified = "2023-12-25"
		item.docstatus = 1
		sync.item = ERPNextItemToSync(item, 1)

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		wc_product.woocommerce_date_modified = "2023-01-01"
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be updated
		mock_update_woocommerce_product.assert_called_once_with(wc_product, sync.item)

	@patch("woocommerce_fusion.tasks.sync.frappe")
	@patch.object(SynchroniseItem, "create_item")
	def test_sync_items_while_passing_item_should_create_item_if_no_item(
		self, mock_create_item, mock_frappe, mock_set_sync_hash, mock_run_item_sync
	):
		"""
		Test that the 'sync_items' function should create a Item if
		there are no corresponding Items
		"""
		# Initialise class
		sync = SynchroniseItem(servers=Mock())

		mock_frappe.get_doc.return_value = Mock()
		mock_frappe.get_single.return_value = Mock()

		woocommerce_server = "site1.example.com"
		woocommerce_id = 1

		# Create dummy WooCommerce Product
		wc_product = frappe.get_doc({"doctype": "WooCommerce Product"})
		wc_product.woocommerce_server = woocommerce_server
		wc_product.id = woocommerce_id
		wc_product.name = generate_woocommerce_record_name_from_domain_and_id(
			woocommerce_server, woocommerce_id
		)
		sync.woocommerce_product = wc_product

		# Call the method under test
		sync.sync_wc_product_with_erpnext_item()

		# Assert that the item need to be created
		mock_create_item.assert_called_once()
		self.assertEqual(mock_create_item.call_args.args[0], wc_product)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_server = "Test Server"
		wc_product.sku = "Test SKU"
		wc_product.woocommerce_id = 12345
		wc_product.woocommerce_name = "Test Product"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		item_mock.append.assert_called_once_with("woocommerce_servers")
		item_mock.insert.assert_called_once()

		self.assertEqual(item_mock.item_code, str(wc_product.woocommerce_id))
		self.assertEqual(item_mock.item_name, wc_product.woocommerce_name)
		self.assertEqual(item_mock.flags.ignore_mandatory, True)

		row = item_mock.append.return_value
		self.assertEqual(row.woocommerce_id, wc_product.woocommerce_id)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item_handles_woo_product_variant(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		parent_item_mock = MagicMock()
		parent_item_mock.item_code = 9999
		parent_item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock
		mock_run_item_sync.return_value = (parent_item_mock, None)

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_id = 42001
		wc_product.woocommerce_server = "Test Server"
		wc_product.type = "variation"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		# Assert that ERPNext item is created as a variant of a template item
		self.assertEqual(item_mock.variant_of, 9999)

	@patch("frappe.get_cached_doc")
	@patch("frappe.new_doc")
	@patch("woocommerce_fusion.tasks.sync_items.json")
	def test_create_item_handles_woo_product_with_variants(
		self, mock_json, mock_new_doc, mock_get_cached_doc, mock_set_sync_hash, mock_run_item_sync
	):
		item_mock = MagicMock()
		item_mock.append.return_value = MagicMock()
		item_mock.woocommerce_servers = [frappe._dict(idx=1, woocommerce_server="Test Server")]
		mock_new_doc.return_value = item_mock

		# Create a mock WooCommerceProduct
		wc_product = MagicMock()
		wc_product.woocommerce_id = 42001
		wc_product.woocommerce_server = "Test Server"
		wc_product.type = "variable"

		# Create instance of the class that contains create_item method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_item method
		sync.create_item(wc_product)

		# Assertions
		mock_new_doc.assert_called_once_with("Item")

		# Assert that ERPNext template item is created
		self.assertEqual(item_mock.has_variants, 1)

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	def test_create_woocommerce_product(
		self,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()
		wc_product_mock.woocommerce_id = 67890

		mock_get_doc.return_value = wc_product_mock
		mock_get_item_price_rate.return_value = "100.00"

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 0
		item_mock.item.variant_of = None

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		mock_get_doc.assert_called_once_with({"doctype": "WooCommerce Product"})

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(
			wc_product_mock.woocommerce_server, item_woocommerce_server_mock.woocommerce_server
		)
		self.assertEqual(wc_product_mock.woocommerce_name, item_mock.item.item_name)
		self.assertEqual(wc_product_mock.type, "simple")
		self.assertEqual(wc_product_mock.regular_price, "100.00")

		self.assertEqual(item_woocommerce_server_mock.woocommerce_id, wc_product_mock.woocommerce_id)
		item_mock.item.save.assert_called_once()

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	def test_create_woocommerce_product_from_variant(
		self,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()

		parent_item_mock = MagicMock()
		parent_item_mock.woocommerce_id = 696969

		mock_get_doc.side_effect = [wc_product_mock, parent_item_mock]

		mock_get_item_price_rate.return_value = "100.00"

		mock_run_item_sync.return_value = (None, parent_item_mock)

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 0
		item_mock.item.variant_of = "696969"

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		expected_calls = [call({"doctype": "WooCommerce Product"}), call("Item", "696969")]
		mock_get_doc.assert_has_calls(expected_calls)

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(wc_product_mock.parent_id, 696969)
		self.assertEqual(wc_product_mock.type, "variation")
		item_mock.item.save.assert_called_once()

	@patch("frappe.get_cached_doc")
	@patch("frappe.get_doc")
	@patch("woocommerce_fusion.tasks.sync_items.get_item_price_rate")
	def test_create_woocommerce_product_from_template_item(
		self,
		mock_get_item_price_rate,
		mock_get_doc,
		mock_get_cached_doc,
		mock_set_sync_hash,
		mock_run_item_sync,
	):
		# Setup mock objects
		wc_product_mock = MagicMock()

		mock_get_doc.return_value = wc_product_mock

		mock_get_item_price_rate.return_value = "100.00"

		# Create a mock ERPNextItemToSync
		item_woocommerce_server_mock = MagicMock()
		item_woocommerce_server_mock.woocommerce_server = "Test Server"
		item_woocommerce_server_mock.enabled = True
		item_woocommerce_server_mock.woocommerce_id = None

		item_mock = MagicMock()
		item_mock.item_woocommerce_server = item_woocommerce_server_mock
		item_mock.item.item_name = "Test Item"
		item_mock.item.has_variants = 1
		item_mock.item.variant_of = None

		mock_run_item_sync.return_value = (item_mock, None)

		# Create instance of the class that contains create_woocommerce_product method
		sync = SynchroniseItem(servers=Mock())

		# Call the create_woocommerce_product method
		sync.create_woocommerce_product(item_mock)

		# Assertions
		mock_get_doc.assert_called_once_with({"doctype": "WooCommerce Product"})

		wc_product_mock.insert.assert_called_once()

		self.assertEqual(wc_product_mock.type, "variable")
		item_mock.item.save.assert_called_once()


@patch("woocommerce_fusion.tasks.sync_items.frappe")
class TestImageSyncToWooCommerce(FrappeTestCase):
	"""
	Tests for the ERPNext → WooCommerce image upload feature.
	Key invariant: no upload should be triggered when the image URL is unchanged.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

	def _make_sync(self, wc_product, item):
		"""Helper: return a SynchroniseItem with item and woocommerce_product set."""
		sync = SynchroniseItem.__new__(SynchroniseItem)
		sync.item = item
		sync.woocommerce_product = wc_product
		sync.settings = Mock()
		return sync

	def _make_item(self, image="/files/product.jpg", last_image_url=None, image_id=None):
		item_wc_server = Mock()
		item_wc_server.get = lambda key, default=None: {
			"woocommerce_last_image_url": last_image_url,
			"woocommerce_image_id": image_id,
			"name": "Item WooCommerce Server-001",
		}.get(key, default)

		item = Mock()
		item.image = image
		item.item_name = "Test Product"

		item_mock = Mock()
		item_mock.item = item
		item_mock.item_woocommerce_server = item_wc_server
		return item_mock

	def _make_wc_product(self, server="site.example.com", wc_id=42):
		wc_product = Mock()
		wc_product.woocommerce_server = server
		wc_product.woocommerce_id = wc_id
		return wc_product

	def test_no_upload_when_image_url_unchanged(self, mock_frappe):
		"""
		When the ERPNext item image URL matches woocommerce_last_image_url,
		_sync_item_image_to_woocommerce must return False and make no API calls.
		"""
		image_url = "https://site.example.com/files/product.jpg"

		wc_server = Mock()
		wc_server.enable_erpnext_to_wc_image_upload = True
		mock_frappe.get_cached_doc.return_value = wc_server
		mock_frappe.db.get_value.return_value = (
			"product.jpg",
			"/files/product.jpg",
			0,  # is_private=0
			"abc123",
			None,
		)
		mock_frappe.utils.get_url.return_value = "https://site.example.com"

		item = self._make_item(
			image="/files/product.jpg",
			last_image_url=image_url,  # same URL as what will be constructed
		)
		wc_product = self._make_wc_product()
		sync = self._make_sync(wc_product, item)

		result = sync._sync_item_image_to_woocommerce(wc_product, item)

		self.assertFalse(result)

	@patch.object(SynchroniseItem, "handle_media_update")
	def test_upload_triggered_when_image_url_changed(self, mock_handle_media, mock_frappe):
		"""
		When the item image URL differs from woocommerce_last_image_url,
		handle_media_update must be called and _sync_item_image_to_woocommerce returns True.
		"""
		wc_server = Mock()
		wc_server.enable_erpnext_to_wc_image_upload = True
		mock_frappe.get_cached_doc.return_value = wc_server
		mock_frappe.db.get_value.return_value = (
			"product-new.jpg",
			"/files/product-new.jpg",
			0,
			"def456",
			None,
		)
		mock_frappe.utils.get_url.return_value = "https://site.example.com"
		mock_frappe.db.set_value = Mock()

		mock_handle_media.return_value = {
			"id": "99",
			"src": "https://site.example.com/wp-content/uploads/product-new.jpg",
			"name": "product-new.jpg",
			"alt": "Test Product",
		}

		item = self._make_item(
			image="/files/product-new.jpg",
			last_image_url="https://site.example.com/files/product-old.jpg",  # old URL
			image_id="55",
		)
		wc_product = self._make_wc_product()
		sync = self._make_sync(wc_product, item)

		result = sync._sync_item_image_to_woocommerce(wc_product, item)

		self.assertTrue(result)
		mock_handle_media.assert_called_once()
		# Verify old image ID was passed for deletion
		call_kwargs = mock_handle_media.call_args.kwargs
		self.assertEqual(call_kwargs["old_image_id"], "55")

	@patch.object(SynchroniseItem, "handle_media_update")
	def test_image_id_and_url_saved_after_upload(self, mock_handle_media, mock_frappe):
		"""
		After a successful upload, woocommerce_image_id and woocommerce_last_image_url
		must be persisted via frappe.db.set_value.
		"""
		wc_server = Mock()
		wc_server.enable_erpnext_to_wc_image_upload = True
		mock_frappe.get_cached_doc.return_value = wc_server
		mock_frappe.db.get_value.return_value = (
			"img.jpg",
			"/files/img.jpg",
			0,
			"hash",
			None,
		)
		mock_frappe.utils.get_url.return_value = "https://site.example.com"
		mock_frappe.db.set_value = Mock()

		mock_handle_media.return_value = {
			"id": "77",
			"src": "https://site.example.com/wp-content/uploads/img.jpg",
			"name": "img.jpg",
			"alt": "Test Product",
		}

		item = self._make_item(image="/files/img.jpg", last_image_url=None)
		wc_product = self._make_wc_product()
		sync = self._make_sync(wc_product, item)

		sync._sync_item_image_to_woocommerce(wc_product, item)

		mock_frappe.db.set_value.assert_called_once()
		call_args = mock_frappe.db.set_value.call_args
		saved_values = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("value")
		self.assertEqual(saved_values["woocommerce_image_id"], "77")
		self.assertEqual(
			saved_values["woocommerce_last_image_url"],
			"https://site.example.com/files/img.jpg",
		)

	@patch.object(SynchroniseItem, "handle_media_update")
	def test_no_upload_when_feature_disabled(self, mock_handle_media, mock_frappe):
		"""
		When enable_erpnext_to_wc_image_upload is False, no upload should occur.
		"""
		wc_server = Mock()
		wc_server.enable_erpnext_to_wc_image_upload = False
		mock_frappe.get_cached_doc.return_value = wc_server

		item = self._make_item(image="/files/product.jpg")
		wc_product = self._make_wc_product()
		sync = self._make_sync(wc_product, item)

		result = sync._sync_item_image_to_woocommerce(wc_product, item)

		self.assertFalse(result)
		mock_handle_media.assert_not_called()

	@patch.object(SynchroniseItem, "handle_media_update")
	def test_no_upload_when_item_has_no_image(self, mock_handle_media, mock_frappe):
		"""
		When the ERPNext item has no image, no upload should occur.
		"""
		wc_server = Mock()
		wc_server.enable_erpnext_to_wc_image_upload = True
		mock_frappe.get_cached_doc.return_value = wc_server

		item = self._make_item(image=None)
		wc_product = self._make_wc_product()
		sync = self._make_sync(wc_product, item)

		result = sync._sync_item_image_to_woocommerce(wc_product, item)

		self.assertFalse(result)
		mock_handle_media.assert_not_called()
