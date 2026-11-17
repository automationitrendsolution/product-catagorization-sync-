"""Product categorisation sync.

Every day at 13:00 IST, merges the four Power BI source tables
(product_categorisation, product_categorisation_gr, product_categorisation_non_us,
product_categorisation_uk) into ``powerbidb.product_categorisation_us_ca_uk_it``,
matching rows on (MarketPlace, Child ASIN), and checks afterwards that every
source row arrived.

Built in the same layout as Amazon_ad_search_term_automation_cron: its lock and
logging modules are reused.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
