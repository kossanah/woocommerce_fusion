import frappe
from frappe.desk.listview import get_list_settings as frappe_get_list_settings


@frappe.whitelist()
def get_list_settings(doctype=None):
	if not doctype:
		return None

	return frappe_get_list_settings(doctype)
