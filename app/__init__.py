"""DRE-Capital — wholesale real estate acquisitions pipeline.

A property pipeline platform for Direct Real Estate Capital (DRE-Capital).
Implements the operator manual workflow:

  1. Build the list  (county records import + list stacking)
  2. Value the house (comps + repair estimate + MAO formula)
  3. Get the phone   (skip tracing)
  4. Make the call    (compliant click-to-call, manual-dial only)
  5. Lock & hand off  (contracts + buyer assignment)

All database persistence lives in app/database.py; domain models in
app/models.py; business logic in app/services/; HTTP routes in app/routers/.
"""
__version__ = "1.0.0"
