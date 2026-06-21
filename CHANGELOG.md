# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-06-21

### Added
- **On-Demand Video Download**: Introduced a manual "Download Video" callback button inside generic video preview cards to optimize server bandwidth and CPU.
- **`fetch_metadata` Permission**: Created a distinct RBAC permission for generic video link detection to check limits prior to fetching metadata.
- **Telegram Hard Size Limits**: Enforced a strict 2GiB upload check before download/upload of generic videos.

### Changed
- **Swapped Video Quotas**: Exchanged default hourly quotas between auto-preview (`preview_video_limit` set to 10/hr) and callback download (`download_video_limit` set to 5/hr).
- **Reduced Default Upload Limit**: Decreased the default automatic preview size limit from 512MB to 256MB.
- **Updated Configurations**: Adjusted default initialization config templates and K3s deployment manifest RBAC permissions.
