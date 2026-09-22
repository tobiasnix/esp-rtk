# SPDX-License-Identifier: AGPL-3.0-only
"""Single cache-busting version for every browser asset."""

ASSET_VERSION = "15.8.0"


def version_html(value):
    return value.replace("ASSET_VERSION", ASSET_VERSION)
