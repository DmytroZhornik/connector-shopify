{
    "name": "ITH Shopify Custom",
    "version": "19.0.1.3.0",
    "summary": "Ireland Tackle Hub custom Shopify connector behaviour",
    "license": "AGPL-3",
    "depends": ["shopify_connector", "queue_job_cron_jobrunner", "sale_stock"],
    "data": [
        "data/external_queue.xml",
        "data/external_queue_cron.xml",
        "views/external_stock_import_views.xml",
        "security/ir.model.access.csv",
        "data/order_mapping.xml",
        "views/order_mapping_views.xml",
        "data/stock_dispatch.xml",
        "data/joint_dispatch.xml",
    ],
    "installable": True,
    "application": False,
}
