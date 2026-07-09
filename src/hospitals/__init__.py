"""Public U.S. hospital knowledge base.

The first data source is the CMS Provider of Services (POS) file, which lists
every Medicare/Medicaid-certified provider in the country. This package
downloads the latest POS file, filters it to active hospitals in a target
state, normalizes identifiers, and loads the result into a SQLite or
PostgreSQL database.

Coverage is built out one state at a time; Kansas and Maryland ship today.
"""

__version__ = "0.1.0"
