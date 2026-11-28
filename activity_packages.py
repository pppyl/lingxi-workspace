"""Compatibility entry point for the activity package CLI and existing imports."""
from workspace.activity_packages import (
    ACTIVITY_ID, ASSET_SUFFIXES, BUNDLED_ACTIVITY_IDS, EXCLUDED, FOLDER_NAME,
    FORMAT_VERSION, MAX_ARCHIVE, MAX_EXPANDED, MAX_FILES, VERSION,
    ActivityManifest, ActivityPackages, ActivityPackageStorage, PlatformError,
    export_package, main, read_manifest,
)

if __name__ == '__main__':
    main()
