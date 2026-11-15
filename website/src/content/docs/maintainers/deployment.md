---
title: Build and manual deployment
---

The portal is built locally and uploaded manually, independently of the Python
application and landing page. Follow the canonical
[build, verification, upload and rollback procedure](https://github.com/noxgle/one/blob/main/website/README.md).

The server needs only static file hosting. No provider credentials, Node.js,
Python runtime or ONE state are needed there. The documentation CI validates
artifacts but has no deployment secrets or publish step. Hosting and the final
domain remain operator decisions.
