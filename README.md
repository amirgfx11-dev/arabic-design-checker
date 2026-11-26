---
title: Arabic Design Checker
emoji: 🔤
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Arabic Design Checker (V1)

Upload a PDF, PPTX or image of an Arabic design. The checker runs fixed code rules (no AI model) and returns a marked-up report:
letter shaping, mixed Arabic/Latin direction, numerals and prices, mirrored layout and alignment, spelling and typography,
brand-guideline match, and page edges.

Optional environment variables (Space settings, Variables and secrets):
- `ACCESS_CODE`: if set, visitors must enter this code first.
- `SECRET_KEY`: any long random string (keeps the login cookie valid across restarts).
- `KEEP_SECONDS`: how long finished reports are kept (default 7200).
