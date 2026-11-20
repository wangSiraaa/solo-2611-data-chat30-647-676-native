"""stratcorr: independent horizon catalogue + borehole correlation repository.

Two independent services:

* :mod:`stratcorr.catalog_service` maintains a finite partial order of
  horizons (``shallower_than``), their acceptable lithology codes and
  immutable, versioned published products.  It never accepts borehole data.
* :mod:`stratcorr.repo_db` consumes published products (local or external)
  and builds an analysis repository: exact borehole-to-horizon correlation,
  per-horizon geometry statistics and sample membership.

All depths are non-negative rational metres represented canonically as
``"num/den"`` strings.  The software performs combinatorial bookkeeping only;
it makes no engineering or geological investigation judgement.
"""

__version__ = "0.1.0"
