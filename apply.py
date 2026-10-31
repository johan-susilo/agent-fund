#!/usr/bin/env python3
"""apply_patches.py - one-shot patcher for the agent-fund "ADD but SELL" contradiction fix.

Usage (from anywhere):
    python apply_patches.py <path-to-agent-fund>            # apply
    python apply_patches.py <path-to-agent-fund> --dry-run  # show what would change, write nothing
    python apply_patches.py <path-to-agent-fund> --revert   # restore the most recent backup

Guarantees
  * all-or-nothing: every anchor is verified in memory first; if ANY patch cannot be applied nothing is written
  * idempotent: patches already applied are detected and skipped
  * backups of every touched file go to <repo>/.patch_backup/<timestamp>/ (use --revert to restore)
  * line endings (LF / CRLF) and UTF-8 are preserved
  * every patched .py file is byte-compiled afterwards; on failure the backup is restored automatically

What it changes
  NEW      src/reconcile.py, src/direction_checks.py
  REPLACE  src/derive_orders.py                  (official NAV, no fee buffer)
  PATCH    src/agents/portfolio_agent.py         (reconcile step, pre-check, day-1 guard, prompt context)
  PATCH    src/agents/check_agent.py             (direction/narrative gate, drop false-positive 減碼)
  PATCH    src/run_daily.py                      (days_held counted once per trade date)
  PATCH    src/prompts/system_prompt.md          (cash_status / gate rules)
"""
from __future__ import annotations

import argparse
import base64
import json
import py_compile
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# ----------------------------------------------------------------------------- embedded new files
EMBEDDED = {
    "src/reconcile.py": (
        "IiIiUG9zdC1nYXRlIHJlY29uY2lsaWF0aW9uOiBrZWVwIGRlY2lzaW9ucywgb3JkZXJzIGFuZCBuYXJyYXRpdmUgdGVsbGluZyB0"
        "aGUgU0FNRSBzdG9yeS4KClJvb3QgY2F1c2UgdGhpcyBmaXhlcyAoMjAyNi0xMS0wMiBydW4pOiBkZXJpdmVfZGVjaXNpb25zKCkg"
        "YXBwbGllcyB0aGUgbm8tdHJhZGUgYmFuZCwgY29zdCBnYXRlLApob2xkaW5nLXBlcmlvZCBydWxlIGFuZCB0dXJub3ZlciBzY2Fs"
        "aW5nIEFGVEVSIHRoZSBMTE0gd3JvdGUgaXRzIHBsYW4uIFRoZSBvbGQKcmVwYWlyX3Bvc3RfYmFuZF9jYXNoKCkgdGhlbiByZXdy"
        "b3RlIHRoZSB0YXJnZXRfd2VpZ2h0IG9mIGFuIHVucmVsYXRlZCAiQUREIiBkZWNpc2lvbiBkb3dud2FyZAp0byBmaXggdGhlIGNh"
        "c2ggd2luZG93LiBSZXN1bHQ6IGFjdGlvbj1BREQsIG9yZGVyPVNFTEwsIGluZmVyZW5jZSB0ZXh0IHNheXMgIuWKoOeivCIsIGFu"
        "ZCB0aGUKZGVjaXNpb24gc3RpbGwgY2FycmllZCB0aGUgc3RhbGUgcmlza19jaGVjay4KCkludmFyaWFudHMgZW5mb3JjZWQgaGVy"
        "ZQogIDEuIGFjdGlvbiBpcyBBTFdBWVMgcmVjb21wdXRlZCBmcm9tIHRoZSByZWFsIHNoYXJlIGRlbHRhIChCVVkvQUREID0+IEJV"
        "WSBvcmRlciwgVFJJTS9TRUxMX0FMTCA9PiBTRUxMIG9yZGVyKS4KICAyLiBUaGUgY2FzaCB3aW5kb3cgaXMgcmVwYWlyZWQgb25s"
        "eSBpbiB0aGUgZGlyZWN0aW9uIHRoZSBwbGFuIGFscmVhZHkgcG9pbnRzOgogICAgICAgY2FzaCB0b28gbG93ICAtPiBjYW5jZWwg"
        "ZGlzY3JldGlvbmFyeSBidXlzLCB0aGVuIHByb21vdGUgdGhlIExMTSdzIE9XTiB0cmltcy9leGl0cywgdGhlbiAobGFzdCByZXNv"
        "cnQpCiAgICAgICAgICAgICAgICAgICAgICAgIGEgcnVsZS1iYXNlZCBwcm8tcmF0YSB0cmltIGRvY3VtZW50ZWQgYnkgYW4gYXV0"
        "by1nZW5lcmF0ZWQgaW5mZXJlbmNlOwogICAgICAgY2FzaCB0b28gaGlnaCAtPiBzY2FsZSB1cCBleGlzdGluZyBCVVkvQUREIG9u"
        "bHkuCiAgICAgQSBCVVkvQUREIGlzIG5ldmVyIHR1cm5lZCBpbnRvIGEgU0VMTC4KICAzLiBXaGVuZXZlciB0aGUgZXhlY3V0ZWQg"
        "cmVzdWx0IGRpZmZlcnMgZnJvbSB3aGF0IGFuIGluZmVyZW5jZSBzYWlkIChibG9ja2VkIGJ5IGEgZ2F0ZSwgY2FuY2VsbGVkIGJ5"
        "IHRoZSBjYXNoCiAgICAgd2luZG93LCAuLi4pIHRoZSBpbmZlcmVuY2UgdGV4dCBpcyBhbm5vdGF0ZWQgd2l0aCDjgJDln7fooYzo"
        "qr/mlbTjgJEgc28gbmFycmF0aXZlID09IG9yZGVycy4KIiIiCmZyb20gX19mdXR1cmVfXyBpbXBvcnQgYW5ub3RhdGlvbnMKCmZy"
        "b20gZGVyaXZlX29yZGVycyBpbXBvcnQgZGVyaXZlX29yZGVycywgcHJvamVjdF9wb3J0Zm9saW8sIHRhcmdldF9zaGFyZXMKZnJv"
        "bSBzZXR0aW5ncyBpbXBvcnQgRkVFX1JBVEUsIFRBWF9SQVRFCgpBVVRPID0gIl9fQVVUT19fIiAgICAgICAgICAjIHBsYWNlaG9s"
        "ZGVyIGluZmVyZW5jZSBpZCBmb3IgcnVsZS1iYXNlZCBzeW50aGV0aWMgdHJpbXMKQ0FTSF9NQVJHSU4gPSAwLjAwMyAgICAgICAg"
        "IyBsYW5kIHRoaXMgZmFyIGluc2lkZSB0aGUgbG93ZXIgY2FzaCBib3VuZCAocHJpY2VzIGRyaWZ0IGJlZm9yZSB0aGUgZmlsbCkK"
        "SE9MRF9EQVlTID0gNSAgICAgICAgICAgICAgIyBzYW1lIG1pbmltdW0gaG9sZGluZyBwZXJpb2QgdXNlZCBpbiBwb3J0Zm9saW9f"
        "YWdlbnQuZGVyaXZlX2RlY2lzaW9ucwoKCmRlZiBfY3V0KHMsIG4pOgogICAgcmV0dXJuIHMgaWYgcyBpcyBOb25lIG9yIGxlbihz"
        "KSA8PSBuIGVsc2Ugc1s6IG4gLSAxXSArICLigKYiCgoKZGVmIF9zaWduKHgpOgogICAgcmV0dXJuICh4ID4gMCkgLSAoeCA8IDAp"
        "CgoKZGVmIF9zY29yZShzY29yZV9tYXAsIHQpOgogICAgaWYgbm90IHNjb3JlX21hcDoKICAgICAgICByZXR1cm4gMC41CiAgICB2"
        "ID0gc2NvcmVfbWFwLmdldCh0KQogICAgcmV0dXJuIDAuNSBpZiB2IGlzIE5vbmUgb3IgdiAhPSB2IGVsc2UgZmxvYXQodikKCgoj"
        "IC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0gcGxhbiBjb250"
        "YWluZXIKY2xhc3MgX1BsYW46CiAgICBkZWYgX19pbml0X18oc2VsZiwgZGVjaXNpb25zLCBub190cmFkZSwgc3RhdGUsIGNsb3Nl"
        "cyk6CiAgICAgICAgc2VsZi5kZWNpc2lvbnMsIHNlbGYubm9fdHJhZGUgPSBsaXN0KGRlY2lzaW9ucyksIGxpc3Qobm9fdHJhZGUp"
        "CiAgICAgICAgc2VsZi5zdGF0ZSwgc2VsZi5jbG9zZXMgPSBzdGF0ZSwgY2xvc2VzCiAgICAgICAgc2VsZi5vcmRlcnMsIHNlbGYu"
        "cmVwID0gW10sIHt9CgogICAgQHByb3BlcnR5CiAgICBkZWYgY2FzaChzZWxmKToKICAgICAgICByZXR1cm4gc2VsZi5yZXBbImNh"
        "c2hfcmF0aW8iXQoKICAgIGRlZiByZWZyZXNoKHNlbGYpOgogICAgICAgICIiIlJlbGFiZWwgZXZlcnkgZGVjaXNpb24gZnJvbSBp"
        "dHMgcmVhbCBzaGFyZSBkZWx0YSwgZHJvcCBuby1vcHMsIHJlLXNpbXVsYXRlLiIiIgogICAgICAgIG5hdiwgaGVsZCA9IHNlbGYu"
        "c3RhdGVbIm5hdiJdLCBzZWxmLnN0YXRlWyJob2xkaW5ncyJdCiAgICAgICAga2VlcCA9IFtdCiAgICAgICAgZm9yIGQgaW4gc2Vs"
        "Zi5kZWNpc2lvbnM6CiAgICAgICAgICAgIHQgPSBkWyJ0aWNrZXIiXQogICAgICAgICAgICBjdXIgPSBoZWxkLmdldCh0LCAwKQog"
        "ICAgICAgICAgICB0Z3QgPSB0YXJnZXRfc2hhcmVzKGRbInRhcmdldF93ZWlnaHQiXSwgbmF2LCBzZWxmLmNsb3Nlc1t0XSkKICAg"
        "ICAgICAgICAgaWYgdGd0ID09IGN1cjoKICAgICAgICAgICAgICAgIGlmIGN1ciA+IDA6CiAgICAgICAgICAgICAgICAgICAgc2Vs"
        "Zi5ub190cmFkZS5hcHBlbmQoeyJ0aWNrZXIiOiB0LCAicmVhc29uX3JlZnMiOiBkWyJpbmZlcmVuY2VfcmVmcyJdWzo1XSwKICAg"
        "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgInJlYXNvbiI6ICLoqr/mlbTlvoznm67mqJnogqHmlbjoiIfn"
        "j77mnInmjIHogqHnm7jlkIzvvIzntq3mjIHkuI3li5UifSkKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIGRb"
        "ImFjdGlvbiJdID0gIlNFTExfQUxMIiBpZiB0Z3QgPT0gMCBlbHNlICgiQlVZIiBpZiBjdXIgPT0gMCBlbHNlICgiQUREIiBpZiB0"
        "Z3QgPiBjdXIgZWxzZSAiVFJJTSIpKQogICAgICAgICAgICBpZiB0Z3QgPT0gMDoKICAgICAgICAgICAgICAgIGRbInRhcmdldF93"
        "ZWlnaHQiXSA9IDAuMAogICAgICAgICAgICBrZWVwLmFwcGVuZChkKQogICAgICAgIHNlbGYuZGVjaXNpb25zID0ga2VlcAogICAg"
        "ICAgIHRyYWRlZCA9IHtkWyJ0aWNrZXIiXSBmb3IgZCBpbiBrZWVwfQogICAgICAgIHNlZW4sIG50ID0gc2V0KCksIFtdCiAgICAg"
        "ICAgZm9yIHggaW4gc2VsZi5ub190cmFkZTogICAgICAgICAgICAgICAgICAgICAgIyBhIHRpY2tlciBtYXkgbm90IGJlIGluIGJv"
        "dGggbGlzdHMsIG5vciB0d2ljZQogICAgICAgICAgICBpZiB4WyJ0aWNrZXIiXSBpbiB0cmFkZWQgb3IgeFsidGlja2VyIl0gaW4g"
        "c2VlbjoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHNlZW4uYWRkKHhbInRpY2tlciJdKQogICAgICAgICAg"
        "ICBudC5hcHBlbmQoeCkKICAgICAgICBzZWxmLm5vX3RyYWRlID0gbnQKICAgICAgICBmb3IgaSwgZCBpbiBlbnVtZXJhdGUoc2Vs"
        "Zi5kZWNpc2lvbnMsIDEpOiAgICAjIGRlcml2ZV9vcmRlcnMgbmVlZHMgdW5pcXVlIGlkcwogICAgICAgICAgICBkWyJkZWNpc2lv"
        "bl9pZCJdID0gZiJEe2l9IgogICAgICAgIHNlbGYub3JkZXJzID0gZGVyaXZlX29yZGVycyhzZWxmLmRlY2lzaW9ucywgc2VsZi5z"
        "dGF0ZSwgc2VsZi5jbG9zZXMpCiAgICAgICAgc2VsZi5yZXAgPSBwcm9qZWN0X3BvcnRmb2xpbyhzZWxmLm9yZGVycywgc2VsZi5z"
        "dGF0ZSwgc2VsZi5jbG9zZXMpCiAgICAgICAgcmV0dXJuIHNlbGYuY2FzaAoKICAgIGRlZiBzbmFwc2hvdChzZWxmKToKICAgICAg"
        "ICByZXR1cm4gbGlzdChzZWxmLmRlY2lzaW9ucyksIGxpc3Qoc2VsZi5ub190cmFkZSkKCiAgICBkZWYgcmVzdG9yZShzZWxmLCBz"
        "bmFwKToKICAgICAgICBzZWxmLmRlY2lzaW9ucywgc2VsZi5ub190cmFkZSA9IGxpc3Qoc25hcFswXSksIGxpc3Qoc25hcFsxXSkK"
        "ICAgICAgICBzZWxmLnJlZnJlc2goKQoKCiMgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
        "LS0tLS0tLS0tLS0tLS0tLSBwcmUtY2hlY2sgZm9yIHRoZSBMTE0gcmV0cnkgbG9vcApkZWYgcHJlY2hlY2tfcGxhbihwb3J0Zm9s"
        "aW8sIGV4aXRzLCBzdGF0ZSwgY2xvc2VzLCBsaW1pdHMpOgogICAgIiIiUmF3IExMTSBwbGFuIG11c3QgYmUgc2VsZi1mdW5kZWQ6"
        "IHBsYW5uZWQgYnV5cyBjYW5ub3QgcHVzaCBjYXNoIGJlbG93IHRoZSB3aW5kb3cuIiIiCiAgICBuYXYsIGhlbGQgPSBzdGF0ZVsi"
        "bmF2Il0sIHN0YXRlWyJob2xkaW5ncyJdCiAgICBjdXJfdyA9IGxhbWJkYSB0OiBoZWxkLmdldCh0LCAwKSAqIGNsb3Nlcy5nZXQo"
        "dCwgMCkgLyBuYXYKICAgIGJ1eSA9IHN1bShtYXgoMC4wLCBwWyJ0YXJnZXRfd2VpZ2h0Il0gLSBjdXJfdyhwWyJ0aWNrZXIiXSkp"
        "IGZvciBwIGluIHBvcnRmb2xpbykKICAgIHNlbGwgPSBzdW0obWF4KDAuMCwgY3VyX3cocFsidGlja2VyIl0pIC0gcFsidGFyZ2V0"
        "X3dlaWdodCJdKSBmb3IgcCBpbiBwb3J0Zm9saW8pCiAgICBzZWxsICs9IHN1bShjdXJfdyhlWyJ0aWNrZXIiXSkgZm9yIGUgaW4g"
        "ZXhpdHMpCiAgICBwcm9qID0gc3RhdGVbImNhc2giXSAvIG5hdiArIHNlbGwgKiAoMSAtIEZFRV9SQVRFIC0gVEFYX1JBVEUpIC0g"
        "YnV5ICogKDEgKyBGRUVfUkFURSkKICAgIGlmIHByb2ogPCBsaW1pdHMuY2FzaF9sbyAtIDAuMDAyOgogICAgICAgIHJldHVybiBb"
        "ZiJ5b3VyIHBsYW4gcHJvamVjdHMgY2FzaCB7cHJvajouMiV9IChidXlzIHtidXk6LjIlfSBOQVYsIHNlbGxzIHtzZWxsOi4yJX0g"
        "TkFWKSB3aGljaCBpcyBiZWxvdyB0aGUgIgogICAgICAgICAgICAgICAgZiJjYXNoIHdpbmRvdyBmbG9vciB7bGltaXRzLmNhc2hf"
        "bG86LjAlfS4gRG8gTk9UIGFkZC9idXkgdW5sZXNzIHlvdSBmdW5kIGl0IHdpdGggZXhwbGljaXQgdHJpbXMgaW4gdGhlICIKICAg"
        "ICAgICAgICAgICAgIGYic2FtZSBwbGFuOyByZWR1Y2UgYnV5cyBieSBhYm91dCB7bGltaXRzLmNhc2hfbG8gLSBwcm9qOi4yJX0g"
        "TkFWLiJdCiAgICByZXR1cm4gW10KCgojIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
        "LS0tLS0tLS0tLS0tLS0gbWFpbiBlbnRyeQpkZWYgYXBwbHkoZGVjaXNpb25zLCBub190cmFkZSwgaW5mcywgbXYsIHBvcnRmb2xp"
        "b19yYXcsIGV4aXRzLCBzdGF0ZSwgY2xvc2VzLCBsaW1pdHMsCiAgICAgICAgICBzY29yZV9tYXA9Tm9uZSwgcm09Tm9uZSwgc2tp"
        "cHBlZD1Ob25lKToKICAgICIiIlJldHVybnMgKGRlY2lzaW9ucywgbm9fdHJhZGUsIG9yZGVycywgcmVwb3J0LCBub3RlcykuIE11"
        "dGF0ZXMgYGluZnNgIChhbm5vdGF0ZXMgLyBhcHBlbmRzIG9uZSBhdXRvIGluZmVyZW5jZSkuIiIiCiAgICBybSA9IHJtIG9yIHsi"
        "VkVUTyI6IHNldCgpfQogICAgbmF2LCBoZWxkID0gc3RhdGVbIm5hdiJdLCBzdGF0ZVsiaG9sZGluZ3MiXQogICAgZGggPSBzdGF0"
        "ZS5nZXQoImRheXNfaGVsZCIsIHt9KQogICAgd2h5ID0ge30KICAgIGZvciBzIGluIHNraXBwZWQgb3IgW106CiAgICAgICAgdCwg"
        "XywgciA9IHMucGFydGl0aW9uKCI6IikKICAgICAgICB3aHlbdC5zdHJpcCgpXSA9IHIuc3RyaXAoKQoKICAgIHBsYW4gPSBfUGxh"
        "bihkZWNpc2lvbnMsIG5vX3RyYWRlLCBzdGF0ZSwgY2xvc2VzKQogICAgcGxhbi5yZWZyZXNoKCkKICAgIGxvLCBoaSA9IGxpbWl0"
        "cy5jYXNoX2xvLCBsaW1pdHMuY2FzaF9oaQogICAgZ29hbCA9IG1pbihsbyArIENBU0hfTUFSR0lOLCAobG8gKyBoaSkgLyAyKQog"
        "ICAgZXZlbnRzLCBub3RlcywgYXV0b191c2VkID0gW10sIFtdLCBGYWxzZQogICAgY3cgPSBsYW1iZGEgdDogaGVsZC5nZXQodCwg"
        "MCkgKiBjbG9zZXNbdF0gLyBuYXYKICAgIGludGVudHMgPSB7cFsidGlja2VyIl06IHAgZm9yIHAgaW4gcG9ydGZvbGlvX3Jhd30K"
        "ICAgIGNfc3RhcnQgPSBwbGFuLmNhc2gKCiAgICBkZWYgYWRkX2RlY2lzaW9uKHQsIGFjdGlvbiwgdywgcmVmcyk6CiAgICAgICAg"
        "cGxhbi5kZWNpc2lvbnMuYXBwZW5kKHsiZGVjaXNpb25faWQiOiAiRDAiLCAidGlja2VyIjogdCwgImFjdGlvbiI6IGFjdGlvbiwg"
        "InRhcmdldF93ZWlnaHQiOiB3LAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgImluZmVyZW5jZV9yZWZzIjogbGlzdChy"
        "ZWZzKVs6OF0sICJyaXNrX2NoZWNrIjogIiJ9KQogICAgICAgIHBsYW4ubm9fdHJhZGUgPSBbeCBmb3IgeCBpbiBwbGFuLm5vX3Ry"
        "YWRlIGlmIHhbInRpY2tlciJdICE9IHRdCiAgICAgICAgcGxhbi5yZWZyZXNoKCkKICAgICAgICByZXR1cm4gYW55KHhbInRpY2tl"
        "ciJdID09IHQgZm9yIHggaW4gcGxhbi5kZWNpc2lvbnMpCgogICAgIyAtLS0tLS0tLS0tLS0tLS0tIGNhc2ggdG9vIExPVzogcmFp"
        "c2UgY2FzaCB3aXRob3V0IGV2ZXIgdHVybmluZyBhIGJ1eSBpbnRvIGEgc2VsbAogICAgaWYgY19zdGFydCA8IGxvOgogICAgICAg"
        "ICMgKGEpIGNhbmNlbCBkaXNjcmV0aW9uYXJ5IGJ1eXMsIHdlYWtlc3Qgc2NvcmUgZmlyc3QKICAgICAgICBmb3IgZCBpbiBzb3J0"
        "ZWQoW3ggZm9yIHggaW4gcGxhbi5kZWNpc2lvbnMgaWYgeFsiYWN0aW9uIl0gaW4gKCJCVVkiLCAiQUREIildLAogICAgICAgICAg"
        "ICAgICAgICAgICAgICBrZXk9bGFtYmRhIHg6IF9zY29yZShzY29yZV9tYXAsIHhbInRpY2tlciJdKSk6CiAgICAgICAgICAgIGlm"
        "IHBsYW4uY2FzaCA+PSBnb2FsOgogICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgdCA9IGRbInRpY2tlciJdCiAgICAg"
        "ICAgICAgIGlmIGhlbGQuZ2V0KHQsIDApID09IDAgYW5kIHBsYW4ucmVwWyJob2xkaW5nc19jb3VudCJdIC0gMSA8IGxpbWl0cy5t"
        "aW5fbjoKICAgICAgICAgICAgICAgIGNvbnRpbnVlICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAjIG5lZWRlZCB0"
        "byByZWFjaCB0aGUgbWluaW11bSBob2xkaW5nIGNvdW50CiAgICAgICAgICAgIGJlZm9yZSA9IHBsYW4uY2FzaAogICAgICAgICAg"
        "ICBwbGFuLmRlY2lzaW9ucy5yZW1vdmUoZCkKICAgICAgICAgICAgaWYgaGVsZC5nZXQodCwgMCkgPiAwOgogICAgICAgICAgICAg"
        "ICAgcGxhbi5ub190cmFkZS5hcHBlbmQoeyJ0aWNrZXIiOiB0LCAicmVhc29uX3JlZnMiOiBkWyJpbmZlcmVuY2VfcmVmcyJdWzo1"
        "XSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAicmVhc29uIjogX2N1dChmIuWKoOeivOW+jOePvumHkeav"
        "lOWwh+eCuiB7YmVmb3JlOi4yJX3vvIzkvY7mlrznj77ph5HnqpfkuIvpmZAge2xvOi4wJX3vvIzlj5bmtojliqDnorzkuKbntq3m"
        "jIHkuI3li5UiLCAzMDApfSkKICAgICAgICAgICAgcGxhbi5yZWZyZXNoKCkKICAgICAgICAgICAgZXZlbnRzLmFwcGVuZCh7InRp"
        "Y2tlciI6IHQsICJyZWZzIjogZFsiaW5mZXJlbmNlX3JlZnMiXSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgInRleHQiOiBm"
        "Int0fSDljp/mk6x7J+WKoOeivCcgaWYgaGVsZC5nZXQodCwgMCkgZWxzZSAn5bu65YCJJ33vvIzmg5/ln7fooYzlvoznj77ph5Hm"
        "r5Qge2JlZm9yZTouMiV9IOS9juaWvOePvumHkeeql+S4i+mZkCAiCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
        "ZiJ7bG86LjAlfe+8jOS+neimj+WJh+WPlua2iO+8jHsn57at5oyB54++5pyJ6YOo5L2NJyBpZiBoZWxkLmdldCh0LCAwKSBlbHNl"
        "ICfkuI3lu7rlgIknfeOAgiJ9KQoKICAgICAgICAjIChiKSBwcm9tb3RlIHRoZSBMTE0ncyBPV04gZXhpdHMgLyB0cmltcyB0aGF0"
        "IHRoZSBiYW5kIG9yIGNvc3QgZ2F0ZSBoYWQgc3dhbGxvd2VkCiAgICAgICAgaWYgcGxhbi5jYXNoIDwgZ29hbDoKICAgICAgICAg"
        "ICAgc29sZCA9IHt4WyJ0aWNrZXIiXSBmb3IgeCBpbiBwbGFuLmRlY2lzaW9uc30KICAgICAgICAgICAgY2FuZHMgPSBbXQogICAg"
        "ICAgICAgICBmb3IgZSBpbiBleGl0czoKICAgICAgICAgICAgICAgIHQgPSBlWyJ0aWNrZXIiXQogICAgICAgICAgICAgICAgaWYg"
        "aGVsZC5nZXQodCwgMCkgPiAwIGFuZCB0IG5vdCBpbiBzb2xkOgogICAgICAgICAgICAgICAgICAgIGNhbmRzLmFwcGVuZCgoMCwg"
        "ZGguZ2V0KHQsIDApIDwgSE9MRF9EQVlTLCBfc2NvcmUoc2NvcmVfbWFwLCB0KSwgdCwgIlNFTExfQUxMIiwgMC4wLCBlWyJpbmZl"
        "cmVuY2VfcmVmcyJdKSkKICAgICAgICAgICAgZm9yIHQsIHAgaW4gaW50ZW50cy5pdGVtcygpOgogICAgICAgICAgICAgICAgY3Vy"
        "ID0gaGVsZC5nZXQodCwgMCkKICAgICAgICAgICAgICAgIHRndCA9IHRhcmdldF9zaGFyZXMocFsidGFyZ2V0X3dlaWdodCJdLCBu"
        "YXYsIGNsb3Nlc1t0XSkKICAgICAgICAgICAgICAgIGlmIGN1ciA+IDAgYW5kIHQgbm90IGluIHNvbGQgYW5kIDAgPCB0Z3QgPCBj"
        "dXI6CiAgICAgICAgICAgICAgICAgICAgY2FuZHMuYXBwZW5kKCgxLCBkaC5nZXQodCwgMCkgPCBIT0xEX0RBWVMsIC0oY3VyIC0g"
        "dGd0KSAqIGNsb3Nlc1t0XSAvIG5hdiwgdCwgIlRSSU0iLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgcFsidGFy"
        "Z2V0X3dlaWdodCJdLCBwWyJpbmZlcmVuY2VfcmVmcyJdKSkKICAgICAgICAgICAgY2FuZHMuc29ydChrZXk9bGFtYmRhIGM6IChj"
        "WzBdLCBjWzFdLCBjWzJdKSkKICAgICAgICAgICAgZm9yIF8sIF8sIF8sIHQsIGFjdCwgdywgcmVmcyBpbiBjYW5kczoKICAgICAg"
        "ICAgICAgICAgIGlmIHBsYW4uY2FzaCA+PSBnb2FsOgogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICBz"
        "bmFwLCBiZWZvcmUgPSBwbGFuLnNuYXBzaG90KCksIHBsYW4uY2FzaAogICAgICAgICAgICAgICAgb2sgPSBhZGRfZGVjaXNpb24o"
        "dCwgYWN0LCB3LCByZWZzKQogICAgICAgICAgICAgICAgaWYgbm90IG9rIG9yIHBsYW4uY2FzaCA+IGhpOgogICAgICAgICAgICAg"
        "ICAgICAgIHBsYW4ucmVzdG9yZShzbmFwKQogICAgICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgICAgICBldmVu"
        "dHMuYXBwZW5kKHsidGlja2VyIjogdCwgInJlZnMiOiByZWZzLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgInRleHQi"
        "OiBmInt0fSDkvp3ljp/mjqjoq5bmhI/lnJZ7J+WHuua4hScgaWYgYWN0ID09ICdTRUxMX0FMTCcgZWxzZSAn5rib56K8J33vvIzk"
        "uKblm6Dnj77ph5Hmr5Qge2JlZm9yZTouMiV9ICIKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZiLkvY7m"
        "lrznj77ph5HnqpfkuIvpmZAge2xvOi4wJX0g6ICM56K65a+m5Z+36KGM77yI5LiN5Y+X54Sh5Lqk5piT5bi277yP5oiQ5pys5qqi"
        "5qC45pSU6Zi777yJ44CCIn0pCgogICAgICAgICMgKGMpIGxhc3QgcmVzb3J0OiBydWxlLWJhc2VkIHByby1yYXRhIHRyaW0sIGRv"
        "Y3VtZW50ZWQgYnkgb25lIGF1dG8gaW5mZXJlbmNlCiAgICAgICAgaWYgcGxhbi5jYXNoIDwgZ29hbDoKICAgICAgICAgICAgc29s"
        "ZCA9IHt4WyJ0aWNrZXIiXSBmb3IgeCBpbiBwbGFuLmRlY2lzaW9uc30KICAgICAgICAgICAgcG9vbCA9IHNvcnRlZChbdCBmb3Ig"
        "dCBpbiBoZWxkIGlmIHQgIT0gIjIzMzAiIGFuZCB0IG5vdCBpbiBzb2xkXSwKICAgICAgICAgICAgICAgICAgICAgICAgICBrZXk9"
        "bGFtYmRhIHQ6IChkaC5nZXQodCwgMCkgPCBIT0xEX0RBWVMsIF9zY29yZShzY29yZV9tYXAsIHQpKSkKICAgICAgICAgICAgZm9y"
        "IHQgaW4gcG9vbDoKICAgICAgICAgICAgICAgIGlmIHBsYW4uY2FzaCA+PSBnb2FsOgogICAgICAgICAgICAgICAgICAgIGJyZWFr"
        "CiAgICAgICAgICAgICAgICBzbmFwID0gcGxhbi5zbmFwc2hvdCgpCiAgICAgICAgICAgICAgICBpZiBub3QgYWRkX2RlY2lzaW9u"
        "KHQsICJUUklNIiwgcm91bmQoY3codCkgKiAwLjc1LCA0KSwgW0FVVE9dKSBvciBwbGFuLmNhc2ggPiBoaToKICAgICAgICAgICAg"
        "ICAgICAgICBwbGFuLnJlc3RvcmUoc25hcCkKICAgICAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICAgICAgYXV0"
        "b191c2VkID0gVHJ1ZQogICAgICAgICAgICBpZiBwbGFuLmNhc2ggPCBsbzoKICAgICAgICAgICAgICAgIG5vdGVzLmFwcGVuZChm"
        "IldBUk5JTkc6IGNhc2gge3BsYW4uY2FzaDouMiV9IHN0aWxsIGJlbG93IHdpbmRvdyBmbG9vciB7bG86LjAlfSBhZnRlciByZWNv"
        "bmNpbGlhdGlvbiIpCgogICAgIyAtLS0tLS0tLS0tLS0tLS0tIGNhc2ggdG9vIEhJR0g6IGRlcGxveSBvbmx5IHRocm91Z2ggZXhp"
        "c3RpbmcgQlVZL0FERCAoZGlyZWN0aW9uIHByZXNlcnZlZCkKICAgIGVsaWYgY19zdGFydCA+IGhpOgogICAgICAgIHRndF9jYXNo"
        "ID0gaGkgLSBDQVNIX01BUkdJTiBpZiBoaSAtIENBU0hfTUFSR0lOID49IGxvIGVsc2UgKGxvICsgaGkpIC8gMgogICAgICAgIGZv"
        "ciBkIGluIHNvcnRlZChbeCBmb3IgeCBpbiBwbGFuLmRlY2lzaW9ucyBpZiB4WyJhY3Rpb24iXSBpbiAoIkJVWSIsICJBREQiKV0s"
        "CiAgICAgICAgICAgICAgICAgICAgICAgIGtleT1sYW1iZGEgeDogLV9zY29yZShzY29yZV9tYXAsIHhbInRpY2tlciJdKSk6CiAg"
        "ICAgICAgICAgIGlmIHBsYW4uY2FzaCA8PSB0Z3RfY2FzaDoKICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgIHQgPSBk"
        "WyJ0aWNrZXIiXQogICAgICAgICAgICByb29tID0gbGltaXRzLmNhcCh0KSAtIDAuMDAyIC0gZFsidGFyZ2V0X3dlaWdodCJdCiAg"
        "ICAgICAgICAgIGlmIHJvb20gPD0gMC4wMDE6CiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBkWyJ0YXJnZXRf"
        "d2VpZ2h0Il0gPSByb3VuZChkWyJ0YXJnZXRfd2VpZ2h0Il0gKyBtaW4ocGxhbi5jYXNoIC0gdGd0X2Nhc2gsIHJvb20pLCA0KQog"
        "ICAgICAgICAgICBwbGFuLnJlZnJlc2goKQoKICAgIGlmIGFicyhwbGFuLmNhc2ggLSBjX3N0YXJ0KSA+IDFlLTk6CiAgICAgICAg"
        "bm90ZXMuYXBwZW5kKGYicmVjb25jaWxlZCBjYXNoIHJhdGlvIHtjX3N0YXJ0Oi4yJX0gLT4ge3BsYW4uY2FzaDouMiV9ICh3aW5k"
        "b3cgW3tsbzouMCV9LCB7aGk6LjAlfV0pIikKCiAgICAjIC0tLS0tLS0tLS0tLS0tLS0gZmluYWxpc2U6IGlkcywgcmVhbGlzZWQg"
        "cmlza19jaGVjaywgb3JkZXJzCiAgICBkZWNpc2lvbnMsIG5vX3RyYWRlID0gcGxhbi5kZWNpc2lvbnMsIHBsYW4ubm9fdHJhZGUK"
        "ICAgIGZvciBpLCBkIGluIGVudW1lcmF0ZShkZWNpc2lvbnMsIDEpOgogICAgICAgIGRbImRlY2lzaW9uX2lkIl0gPSBmIkR7aX0i"
        "CiAgICBvcmRlcnMgPSBkZXJpdmVfb3JkZXJzKGRlY2lzaW9ucywgc3RhdGUsIGNsb3NlcykKICAgIHJlcG9ydCA9IHByb2plY3Rf"
        "cG9ydGZvbGlvKG9yZGVycywgc3RhdGUsIGNsb3NlcykKICAgIGRlbHRhID0ge29bInRpY2tlciJdOiAob1sic2hhcmVzIl0gaWYg"
        "b1sic2lkZSJdID09ICJCVVkiIGVsc2UgLW9bInNoYXJlcyJdKSBmb3IgbyBpbiBvcmRlcnN9CiAgICBmb3IgZCBpbiBkZWNpc2lv"
        "bnM6CiAgICAgICAgdCA9IGRbInRpY2tlciJdCiAgICAgICAgd19yZWFsID0gKGhlbGQuZ2V0KHQsIDApICsgZGVsdGFbdF0pICog"
        "Y2xvc2VzW3RdIC8gbmF2CiAgICAgICAgb2ZmaWNpYWwgPSAiMjUlIiBpZiB0ID09ICIyMzMwIiBlbHNlICIxMCUiCiAgICAgICAg"
        "ZFsicmlza19jaGVjayJdID0gKCLlh7rmuIXmjIHogqHvvIzph4vlh7ros4fph5EiIGlmIGRbImFjdGlvbiJdID09ICJTRUxMX0FM"
        "TCIgZWxzZQogICAgICAgICAgICAgICAgICAgICAgICAgICBmIntkWydhY3Rpb24nXX3vvJrlr6bpmpvln7fooYzlvozmrIrph40g"
        "e3dfcmVhbDouMiV9IOKJpCDlhafpg6jkuIrpmZAge2xpbWl0cy5jYXAodCk6LjElfe+8iOWumOaWuSB7b2ZmaWNpYWx977yJIikK"
        "CiAgICAjIC0tLS0tLS0tLS0tLS0tLS0gYXV0byBpbmZlcmVuY2UgZm9yIHJ1bGUtYmFzZWQgdHJpbXMKICAgIGlmIGF1dG9fdXNl"
        "ZDoKICAgICAgICBhdXRvX2lkID0gZiJJe2xlbihpbmZzKSArIDF9IgogICAgICAgIHRyaW1tZWQgPSBbZFsidGlja2VyIl0gZm9y"
        "IGQgaW4gZGVjaXNpb25zIGlmIEFVVE8gaW4gZFsiaW5mZXJlbmNlX3JlZnMiXV0KICAgICAgICBmb3IgZCBpbiBkZWNpc2lvbnM6"
        "CiAgICAgICAgICAgIGRbImluZmVyZW5jZV9yZWZzIl0gPSBbYXV0b19pZCBpZiByID09IEFVVE8gZWxzZSByIGZvciByIGluIGRb"
        "ImluZmVyZW5jZV9yZWZzIl1dCiAgICAgICAgaW5mcy5hcHBlbmQoeyJpbmZfaWQiOiBhdXRvX2lkLCAicHJlbWlzZV9yZWZzIjog"
        "bGlzdChtdi5nZXQoImJhc2lzX3JlZnMiLCBbXSkpWzozXSwKICAgICAgICAgICAgICAgICAgICAgImxvZ2ljIjogX2N1dChmIuWf"
        "t+ihjOWxpOePvumHkeeql+iqv+evgO+8muioiOeVq+e2k+eEoeS6pOaYk+W4tuiIh+aIkOacrOaqouaguOW+jO+8jOmgkOS8sOeP"
        "vumHkeavlCB7Y19zdGFydDouMiV9IOS9juaWvOePvumHkeeql+S4i+mZkCB7bG86LjAlfe+8jCIKICAgICAgICAgICAgICAgICAg"
        "ICAgICAgICAgICAgICAgICBmIuS4lOWOn+aOqOirluaJgOaMh+a4m+eivOS4jei2s+S7peijnOi2s++8m+S+nemgkOWFiOWumue+"
        "qeS5i+mHj+WMluimj+WJh+aMieipleWIhueUseS9juiHs+mrmOOAgeWQhOa4m+eivOe0hOWbm+WIhuS5i+S4gOaMgeiCoe+8miIK"
        "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmInsn44CBJy5qb2luKHRyaW1tZWQpfeOAguatpOeCuuapn+aisOW8"
        "j+ePvumHkeiqv+evgO+8jOmdnuaWsOWinuW4guWgtOWIpOaWt+OAgiIsIDEwMDApLAogICAgICAgICAgICAgICAgICAgICAiY29u"
        "Y2x1c2lvbiI6IF9jdXQoZiLmuJvnorwgeyfjgIEnLmpvaW4odHJpbW1lZCl9IOS7peWbnuWIsOePvumHkeeql+OAgiIsIDMwMCks"
        "CiAgICAgICAgICAgICAgICAgICAgICJjb3VudGVyX2V2aWRlbmNlIjogIua4m+eivOWwh+mZjeS9juaXouaciemDqOS9jeeahOi2"
        "qOWLouaatOmcsu+8jOS4pueUoueUn+S6pOaYk+aIkOacrOOAgiIsICJjb25maWRlbmNlIjogMC42fSkKCiAgICAjIC0tLS0tLS0t"
        "LS0tLS0tLS0gcmVzaWR1YWwgbWlzbWF0Y2hlczogaW50ZW5kZWQgdnMgZXhlY3V0ZWQKICAgIGV2ZW50ZWQgPSB7ZVsidGlja2Vy"
        "Il0gZm9yIGUgaW4gZXZlbnRzfQogICAgZm9yIHAgaW4gcG9ydGZvbGlvX3JhdzoKICAgICAgICB0LCB3ID0gcFsidGlja2VyIl0s"
        "IHBbInRhcmdldF93ZWlnaHQiXQogICAgICAgIGN1ciA9IGhlbGQuZ2V0KHQsIDApCiAgICAgICAgd2FudCA9IF9zaWduKHRhcmdl"
        "dF9zaGFyZXModywgbmF2LCBjbG9zZXNbdF0pIC0gY3VyKQogICAgICAgIG1hdGVyaWFsID0gY3VyID09IDAgb3IgYWJzKHcgLSBj"
        "dyh0KSkgPiBtYXgoMC4wMDMsIG1pbih3ICogMC4yMCwgbGltaXRzLm5vX3RyYWRlX2JhbmQpKQogICAgICAgIGdvdCA9IF9zaWdu"
        "KGRlbHRhLmdldCh0LCAwKSkKICAgICAgICBpZiB3YW50IGFuZCBtYXRlcmlhbCBhbmQgZ290ICE9IHdhbnQgYW5kIHQgbm90IGlu"
        "IGV2ZW50ZWQ6CiAgICAgICAgICAgIHJlYXNvbiA9IHdoeS5nZXQodCkgb3IgIuacqumAmumBjueEoeS6pOaYk+W4tu+8j+aIkOac"
        "rO+8j+aMgeacieacn++8j+ePvumHkeeql+aqouaguCIKICAgICAgICAgICAgZXZlbnRzLmFwcGVuZCh7InRpY2tlciI6IHQsICJy"
        "ZWZzIjogcFsiaW5mZXJlbmNlX3JlZnMiXSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgInRleHQiOiBmInt0fSDljp/mk6x7"
        "J+WKoOeivCcgaWYgd2FudCA+IDAgZWxzZSAn5rib56K8J33vvIzlr6bpmpvmnKrln7fooYzvvIh7cmVhc29ufe+8ie+8jOe2reaM"
        "geePvuaciemDqOS9jeOAgiJ9KQogICAgZm9yIGUgaW4gZXhpdHM6CiAgICAgICAgdCA9IGVbInRpY2tlciJdCiAgICAgICAgaWYg"
        "aGVsZC5nZXQodCwgMCkgPiAwIGFuZCB0IG5vdCBpbiBldmVudGVkIGFuZCBkZWx0YS5nZXQodCwgMCkgIT0gLWhlbGRbdF06CiAg"
        "ICAgICAgICAgIHJlYXNvbiA9IHdoeS5nZXQodCkgb3IgIuacqumAmumBjuaIkOacrO+8j+aMgeacieacn+aqouaguCIKICAgICAg"
        "ICAgICAgZXZlbnRzLmFwcGVuZCh7InRpY2tlciI6IHQsICJyZWZzIjogZVsiaW5mZXJlbmNlX3JlZnMiXSwKICAgICAgICAgICAg"
        "ICAgICAgICAgICAgICAgInRleHQiOiBmInt0fSDljp/mk6zlh7rmuIXvvIzlr6bpmpvmnKrln7fooYzvvIh7cmVhc29ufe+8ie+8"
        "jOe6jOaKseOAgiJ9KQoKICAgIGZvciBldiBpbiBldmVudHM6CiAgICAgICAgZm9yIHJlZiBpbiBkaWN0LmZyb21rZXlzKGV2WyJy"
        "ZWZzIl0pOgogICAgICAgICAgICBpbmYgPSBuZXh0KChpIGZvciBpIGluIGluZnMgaWYgaVsiaW5mX2lkIl0gPT0gcmVmKSwgTm9u"
        "ZSkKICAgICAgICAgICAgaWYgbm90IGluZiBvciByZWYuc3RhcnRzd2l0aCgiSSIpIGlzIEZhbHNlOgogICAgICAgICAgICAgICAg"
        "Y29udGludWUKICAgICAgICAgICAgaWYgYXV0b191c2VkIGFuZCBpbmZbImluZl9pZCJdID09IGluZnNbLTFdWyJpbmZfaWQiXSBh"
        "bmQgIuePvumHkeeql+iqv+evgCIgaW4gaW5mWyJsb2dpYyJdOgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAg"
        "aW5mWyJsb2dpYyJdID0gX2N1dCgoaW5mWyJsb2dpYyJdIG9yICIiKSArICIg44CQ5Z+36KGM6Kq/5pW044CRIiArIGV2WyJ0ZXh0"
        "Il0sIDEwMDApCiAgICAgICAgICAgIHN1ZmZpeCA9ICLvvZzln7fooYzoqr/mlbTvvJoiICsgZXZbInRleHQiXQogICAgICAgICAg"
        "ICBzdWZmaXggPSBzdWZmaXhbOjI5OV0KICAgICAgICAgICAgaW5mWyJjb25jbHVzaW9uIl0gPSAoaW5mLmdldCgiY29uY2x1c2lv"
        "biIpIG9yICIiKVs6IDMwMCAtIGxlbihzdWZmaXgpXSArIHN1ZmZpeAogICAgICAgIG5vdGVzLmFwcGVuZCgibmFycmF0aXZlIGFs"
        "aWduZWQ6ICIgKyBldlsidGV4dCJdKQogICAgcmV0dXJuIGRlY2lzaW9ucywgbm9fdHJhZGUsIG9yZGVycywgcmVwb3J0LCBub3Rl"
        "cwo="
    ),
    "src/direction_checks.py": (
        "IiIiRmluYWwtZ2F0ZSBjaGVja3MgdGhhdCBkZWNpc2lvbnMsIG9yZGVycyBhbmQgbmFycmF0aXZlIGFncmVlLiBJbXBvcnRlZCBi"
        "eSBjaGVja19hZ2VudC5maW5hbF9nYXRlKCkuIiIiCmZyb20gX19mdXR1cmVfXyBpbXBvcnQgYW5ub3RhdGlvbnMKaW1wb3J0IHJl"
        "CgpfQUREID0gciIoPzrliqDnorx86LK36YCyfOW7uuWAiXzntI3lhaV85paw5aKeKSIKX0VYSVQgPSByIig/OuWHuua4hXzmjpLp"
        "maR85YmU6ZmkfOa4heWAiSkiCl9ORUcgPSByIig/OuS4jXzmnKp85Yu/fOmBv+WFjXzmmqvnt6l854Sh6ZyAfOS4jeS6iCkiCgoK"
        "ZGVmIGNoZWNrX2RpcmVjdGlvbihkOiBkaWN0KSAtPiBsaXN0OgogICAgIiIiRVJST1JTOiBhY3Rpb24gbGFiZWwgbXVzdCBtYXRj"
        "aCB0aGUgb3JkZXIgc2lkZTsgZXZlcnkgZGVjaXNpb24gbmVlZHMgZXhhY3RseSBvbmUgb3JkZXIuIiIiCiAgICBlcnJzLCBieV9p"
        "ZCA9IFtdLCB7eFsiZGVjaXNpb25faWQiXTogeCBmb3IgeCBpbiBkWyJkZWNpc2lvbnMiXX0KICAgIG5fb3JkZXJzID0ge30KICAg"
        "IGZvciBvIGluIGRbIm9yZGVycyJdOgogICAgICAgIGRlYyA9IGJ5X2lkLmdldChvWyJkZWNpc2lvbl9yZWYiXSkKICAgICAgICBp"
        "ZiBub3QgZGVjOgogICAgICAgICAgICBjb250aW51ZQogICAgICAgIG5fb3JkZXJzW2RlY1siZGVjaXNpb25faWQiXV0gPSBuX29y"
        "ZGVycy5nZXQoZGVjWyJkZWNpc2lvbl9pZCJdLCAwKSArIDEKICAgICAgICBidXlfYWN0aW9uID0gZGVjWyJhY3Rpb24iXSBpbiAo"
        "IkJVWSIsICJBREQiKQogICAgICAgIGlmIGJ1eV9hY3Rpb24gIT0gKG9bInNpZGUiXSA9PSAiQlVZIik6CiAgICAgICAgICAgIGVy"
        "cnMuYXBwZW5kKGYiRElSRUNUSU9OOiB7ZGVjWydkZWNpc2lvbl9pZCddfSB7ZGVjWyd0aWNrZXInXX0gYWN0aW9uPXtkZWNbJ2Fj"
        "dGlvbiddfSBidXQgb3JkZXIgc2lkZT17b1snc2lkZSddfSIpCiAgICBmb3IgZGlkLCBkZWMgaW4gYnlfaWQuaXRlbXMoKToKICAg"
        "ICAgICBpZiBuX29yZGVycy5nZXQoZGlkLCAwKSAhPSAxOgogICAgICAgICAgICBlcnJzLmFwcGVuZChmIntkaWR9IHtkZWNbJ3Rp"
        "Y2tlciddfSBoYXMge25fb3JkZXJzLmdldChkaWQsIDApfSBvcmRlcnMgKGV4cGVjdGVkIGV4YWN0bHkgMSkiKQogICAgcmV0dXJu"
        "IGVycnMKCgpkZWYgY2hlY2tfbmFycmF0aXZlX3ZzX29yZGVycyhkOiBkaWN0LCBzdGF0ZTogZGljdCkgLT4gbGlzdDoKICAgICIi"
        "IldBUk5JTkdTIChoZXVyaXN0aWMsIG5ldmVyIGJsb2Nrcyk6IGluZmVyZW5jZSB0ZXh0IHByb21pc2VzIGEgbW92ZSB0aGUgb3Jk"
        "ZXJzIGRvIG5vdCBtYWtlLiIiIgogICAgd2FybnMgPSBbXQogICAgc2VsbHMgPSB7b1sidGlja2VyIl0gZm9yIG8gaW4gZFsib3Jk"
        "ZXJzIl0gaWYgb1sic2lkZSJdID09ICJTRUxMIn0KICAgIGJ1eXMgPSB7b1sidGlja2VyIl0gZm9yIG8gaW4gZFsib3JkZXJzIl0g"
        "aWYgb1sic2lkZSJdID09ICJCVVkifQogICAgZm9yIGluZiBpbiBkWyJpbmZlcmVuY2VzIl06CiAgICAgICAgdGV4dCA9IGYie2lu"
        "Zi5nZXQoJ2xvZ2ljJywgJycpfSB7aW5mLmdldCgnY29uY2x1c2lvbicsICcnKX0iCiAgICAgICAgaWYgIuWft+ihjOiqv+aVtCIg"
        "aW4gdGV4dDoKICAgICAgICAgICAgY29udGludWUgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICMgYWxyZWFkeSBy"
        "ZWNvbmNpbGVkIGJ5IHJlY29uY2lsZS5hcHBseQogICAgICAgIGZvciBtIGluIHJlLmZpbmRpdGVyKHJmIih7X05FR30pP1xzKntf"
        "QUREfVteMC0544CC77ybXXt7MCwxMn19PyhcZHt7NH19KSIsIHRleHQpOgogICAgICAgICAgICB0ID0gbS5ncm91cCgyKQogICAg"
        "ICAgICAgICBpZiBub3QgbS5ncm91cCgxKSBhbmQgdCBpbiBzZWxsczoKICAgICAgICAgICAgICAgIHdhcm5zLmFwcGVuZChmIk5B"
        "UlJBVElWRToge2luZlsnaW5mX2lkJ119IHNheXMgYWRkL2J1eSB7dH0gYnV0IHRoZSBvcmRlciBpcyBTRUxMIikKICAgICAgICBm"
        "b3IgbSBpbiByZS5maW5kaXRlcihyZiIoe19ORUd9KT9ccyp7X0VYSVR9W14wLTnjgILvvJtde3swLDEyfX0/KFxke3s0fX0pIiwg"
        "dGV4dCk6CiAgICAgICAgICAgIHQgPSBtLmdyb3VwKDIpCiAgICAgICAgICAgIGlmIG5vdCBtLmdyb3VwKDEpIGFuZCB0IGluIHN0"
        "YXRlWyJob2xkaW5ncyJdIGFuZCB0IG5vdCBpbiBzZWxsczoKICAgICAgICAgICAgICAgIHdhcm5zLmFwcGVuZChmIk5BUlJBVElW"
        "RToge2luZlsnaW5mX2lkJ119IHNheXMgZXhpdCB7dH0gYnV0IHRoZXJlIGlzIG5vIFNFTEwgb3JkZXIiKQogICAgcmV0dXJuIHdh"
        "cm5zCg=="
    ),
    "src/derive_orders.py": (
        "IiIiT2ZmaWNpYWwgb3JkZXIgZGVyaXZhdGlvbiBhbmQgcG9zdC10cmFkZSBwcm9qZWN0aW9uLgoKVGhlIExMTSBvbmx5IHN1cHBs"
        "aWVzIHRhcmdldCB3ZWlnaHRzOyBzaGFyZSBjb3VudHMgYWx3YXlzIGNvbWUgZnJvbSB0aGlzIG1vZHVsZSBhbmQgYXJlCm5ldmVy"
        "IGhhbmQtYWRqdXN0ZWQuIFJ1bGUgY2hlY2tzIGxpdmUgaW4gcmlza19hZ2VudC5weS4KCkNIQU5HRSB2cyBwcmV2aW91cyB2ZXJz"
        "aW9uOiB0YXJnZXQgc2hhcmVzIGFyZSBjb21wdXRlZCBmcm9tIHRoZSBPRkZJQ0lBTCBwcmV2aW91cy1kYXkgTkFWCihgc3RhdGVb"
        "Im5hdiJdYCA9IERhdGFQYWNrIG5hdl90cmFkYWJsZSkuIFRoZSBvbGQgY29kZSBtdWx0aXBsaWVkIE5BViBieSAoMSAtIGZlZSAt"
        "IHRheCksIHdoaWNoCm1hZGUgc3VibWl0dGVkIHNoYXJlIGNvdW50cyBkaWZmZXIgZnJvbSB0aGUgc2VydmVyJ3Mgb2ZmaWNpYWwg"
        "ZGVyaXZhdGlvbiAoZS5nLiAyNjE1IG9uIDIwMjYtMTEtMDI6CnN1Ym1pdHRlZCBTRUxMIDEyOCwwMDAsIG9mZmljaWFsIFNFTEwg"
        "MTI3LDAwMCkuIEZlZSBzYWZldHkgaXMgYWxyZWFkeSBwcm92aWRlZCBieSB0aGUgY2FzaCB3aW5kb3cKYW5kIHRoZSBzdW1fbG8v"
        "c3VtX2hpIG1hcmdpbnMgaW4gc2V0dGluZ3MuTGltaXRzLgoiIiIKZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoK"
        "aW1wb3J0IG1hdGgKCmZyb20gc2V0dGluZ3MgaW1wb3J0IEZFRV9SQVRFLCBMT1QsIFRBWF9SQVRFCgoKZGVmIHRhcmdldF9zaGFy"
        "ZXMod2VpZ2h0OiBmbG9hdCwgbmF2OiBmbG9hdCwgcHJpY2U6IGZsb2F0KSAtPiBpbnQ6CiAgICAiIiJPZmZpY2lhbCBmb3JtdWxh"
        "OiBmbG9vcih3ZWlnaHQgeCBwcmV2IE5BViAvIHByZXYgY2xvc2UgLyAxMDAwKSB4IDEwMDAuIiIiCiAgICByZXR1cm4gbWF0aC5m"
        "bG9vcih3ZWlnaHQgKiBuYXYgLyBwcmljZSAvIExPVCkgKiBMT1QKCgpkZWYgbG90X3dlaWdodChuYXY6IGZsb2F0LCBwcmljZTog"
        "ZmxvYXQpIC0+IGZsb2F0OgogICAgIiIiTkFWIHNoYXJlIG9mIGEgc2luZ2xlIGJvYXJkIGxvdDsgYSB0YXJnZXQgd2VpZ2h0IGJl"
        "bG93IHRoaXMgYnV5cyB6ZXJvIHNoYXJlcy4iIiIKICAgIHJldHVybiBMT1QgKiBwcmljZSAvIG5hdgoKCmRlZiBkZXJpdmVfb3Jk"
        "ZXJzKGRlY2lzaW9uczogbGlzdCwgc3RhdGU6IGRpY3QsIGNsb3NlczogZGljdCkgLT4gbGlzdDoKICAgICIiImRlY2lzaW9uczog"
        "W3siZGVjaXNpb25faWQiLCJ0aWNrZXIiLCJhY3Rpb24iLCJ0YXJnZXRfd2VpZ2h0In1dOyBjbG9zZXM6IHByZXZpb3VzIGNsb3Nl"
        "IHBlciB0aWNrZXIuIiIiCiAgICBvcmRlcnMgPSBbXQogICAgZm9yIGQgaW4gZGVjaXNpb25zOgogICAgICAgIHQgPSBkWyJ0aWNr"
        "ZXIiXQogICAgICAgIGlmIHQgbm90IGluIGNsb3NlczoKICAgICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcihmInt0fTogbm8gcHJl"
        "dmlvdXMgY2xvc2UgYXZhaWxhYmxlIikKICAgICAgICB0Z3QgPSB0YXJnZXRfc2hhcmVzKGRbInRhcmdldF93ZWlnaHQiXSwgc3Rh"
        "dGVbIm5hdiJdLCBjbG9zZXNbdF0pICAgIyBvZmZpY2lhbCBOQVYsIG5vIGJ1ZmZlcgogICAgICAgIGN1ciA9IHN0YXRlWyJob2xk"
        "aW5ncyJdLmdldCh0LCAwKQogICAgICAgIGRpZmYgPSB0Z3QgLSBjdXIKICAgICAgICBpZiBkaWZmID09IDA6CiAgICAgICAgICAg"
        "IHJhaXNlIFZhbHVlRXJyb3IoZiJ7ZFsnZGVjaXNpb25faWQnXX0ge3R9OiB0YXJnZXQgZXF1YWxzIGN1cnJlbnQgaG9sZGluZzsg"
        "dXNlIG5vX3RyYWRlX2RlY2lzaW9ucyIpCiAgICAgICAgc2lkZSA9ICJCVVkiIGlmIGRpZmYgPiAwIGVsc2UgIlNFTEwiCiAgICAg"
        "ICAgaWYgc2lkZSA9PSAiU0VMTCIgYW5kIC1kaWZmID4gY3VyOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKGYie3R9OiBz"
        "ZWxsaW5nIHstZGlmZn0gZXhjZWVkcyBob2xkaW5nIHtjdXJ9IChvdmVyc2VsbCkiKQogICAgICAgIG9yZGVycy5hcHBlbmQoewog"
        "ICAgICAgICAgICAidGlja2VyIjogdCwgInNpZGUiOiBzaWRlLCAic2hhcmVzIjogYWJzKGRpZmYpLCAiZGVjaXNpb25fcmVmIjog"
        "ZFsiZGVjaXNpb25faWQiXSwKICAgICAgICAgICAgImRlcml2YXRpb25fZGV0YWlsIjogKGYidGFyZ2V0X3NoYXJlcyA9IGZsb29y"
        "KHtkWyd0YXJnZXRfd2VpZ2h0J119w5d7c3RhdGVbJ25hdiddOiwuMGZ9IC8gIgogICAgICAgICAgICAgICAgICAgICAgICAgICAg"
        "ICAgICAgZiJ7Y2xvc2VzW3RdfSAvIDEwMDApw5cxMDAwID0ge3RndDosfe+8m+ePvuaMgSB7Y3VyOix9IOKGkiB7c2lkZX0ge2Fi"
        "cyhkaWZmKTosfSIpLAogICAgICAgIH0pCiAgICByZXR1cm4gb3JkZXJzCgoKZGVmIHByb2plY3RfcG9ydGZvbGlvKG9yZGVyczog"
        "bGlzdCwgc3RhdGU6IGRpY3QsIGNsb3NlczogZGljdCkgLT4gZGljdDoKICAgICIiIkVzdGltYXRlIHRoZSBwb3N0LXRyYWRlIHBv"
        "cnRmb2xpbyBhdCBwcmV2aW91cy1jbG9zZSBwcmljZXMgKGZlZXMgYW5kIHRheCBkZWR1Y3RlZCBmcm9tIGNhc2gpLiIiIgogICAg"
        "c2hhcmVzLCBjYXNoID0gZGljdChzdGF0ZVsiaG9sZGluZ3MiXSksIHN0YXRlWyJjYXNoIl0KICAgIGJ1eV9hbXQgPSBzZWxsX2Ft"
        "dCA9IGZlZXMgPSAwLjAKICAgIGZvciBvIGluIG9yZGVyczoKICAgICAgICBhbXQgPSBvWyJzaGFyZXMiXSAqIGNsb3Nlc1tvWyJ0"
        "aWNrZXIiXV0KICAgICAgICBpZiBvWyJzaWRlIl0gPT0gIkJVWSI6CiAgICAgICAgICAgIHNoYXJlc1tvWyJ0aWNrZXIiXV0gPSBz"
        "aGFyZXMuZ2V0KG9bInRpY2tlciJdLCAwKSArIG9bInNoYXJlcyJdCiAgICAgICAgICAgIGNhc2ggLT0gYW10ICogKDEgKyBGRUVf"
        "UkFURSkKICAgICAgICAgICAgZmVlcyArPSBhbXQgKiBGRUVfUkFURQogICAgICAgICAgICBidXlfYW10ICs9IGFtdAogICAgICAg"
        "IGVsc2U6CiAgICAgICAgICAgIHNoYXJlc1tvWyJ0aWNrZXIiXV0gPSBzaGFyZXMuZ2V0KG9bInRpY2tlciJdLCAwKSAtIG9bInNo"
        "YXJlcyJdCiAgICAgICAgICAgIGNhc2ggKz0gYW10ICogKDEgLSBGRUVfUkFURSAtIFRBWF9SQVRFKQogICAgICAgICAgICBmZWVz"
        "ICs9IGFtdCAqIChGRUVfUkFURSArIFRBWF9SQVRFKQogICAgICAgICAgICBzZWxsX2FtdCArPSBhbXQKICAgIHNoYXJlcyA9IHt0"
        "OiBzIGZvciB0LCBzIGluIHNoYXJlcy5pdGVtcygpIGlmIHMgPiAwfQogICAgdmFsdWVzID0ge3Q6IHMgKiBjbG9zZXNbdF0gZm9y"
        "IHQsIHMgaW4gc2hhcmVzLml0ZW1zKCl9CiAgICBuYXYgPSBjYXNoICsgc3VtKHZhbHVlcy52YWx1ZXMoKSkKICAgIHJldHVybiB7"
        "CiAgICAgICAgImhvbGRpbmdzX2NvdW50IjogbGVuKHNoYXJlcyksCiAgICAgICAgImNhc2giOiBjYXNoLAogICAgICAgICJjYXNo"
        "X3JhdGlvIjogY2FzaCAvIG5hdiwKICAgICAgICAibmF2X2FmdGVyIjogbmF2LAogICAgICAgICJidXlfYW10IjogYnV5X2FtdCwK"
        "ICAgICAgICAic2VsbF9hbXQiOiBzZWxsX2FtdCwKICAgICAgICAibmV0X2Zsb3dfcGN0X25hdiI6IChidXlfYW10IC0gc2VsbF9h"
        "bXQpIC8gc3RhdGVbIm5hdiJdLAogICAgICAgICJ3ZWlnaHRzIjogZGljdChzb3J0ZWQoKCh0LCB2IC8gbmF2KSBmb3IgdCwgdiBp"
        "biB2YWx1ZXMuaXRlbXMoKSksIGtleT1sYW1iZGEgeDogLXhbMV0pKSwKICAgIH0KCgpkZWYgY29zdF9nYXRlKHNpZGUsIHNoYXJl"
        "cywgcHJpY2UsIG5hdiwgc2NvcmUsIGxpbWl0cywgZm9yY2VkPUZhbHNlLCBjaGVja19zaXplPVRydWUpOgogICAgIiIiKG9rLCBy"
        "ZWFzb24pLiBEaXNjcmV0aW9uYXJ5IHRyYWRlcyBtdXN0IGJlYXQgdGhlaXIgb3duIGZlZTsgZm9yY2VkIHRyYWRlcyBhbHdheXMg"
        "cGFzcy4iIiIKICAgIGlmIGZvcmNlZDoKICAgICAgICByZXR1cm4gVHJ1ZSwgIiIKICAgIGFtdCA9IHNoYXJlcyAqIHByaWNlCiAg"
        "ICBjb3N0ID0gYW10ICogKEZFRV9SQVRFIGlmIHNpZGUgPT0gIkJVWSIgZWxzZSBGRUVfUkFURSArIFRBWF9SQVRFKQogICAgaWYg"
        "Y2hlY2tfc2l6ZSBhbmQgYW10IC8gbmF2IDwgbGltaXRzLm1pbl90cmFkZV93OgogICAgICAgIHJldHVybiBGYWxzZSwgZiLmiJDk"
        "uqTph5HpoY3lg4Uge2FtdCAvIG5hdjouMiV9IE5BVu+8jOS9juaWvOacgOWwj+S6pOaYk+mWgOaquyB7bGltaXRzLm1pbl90cmFk"
        "ZV93Oi4xJX0iCiAgICBpZiBzY29yZSBpcyBOb25lOgogICAgICAgIHJldHVybiBUcnVlLCAiIgogICAgZWRnZSA9IGxpbWl0cy5h"
        "bHBoYV9zY2FsZSAqIChzY29yZSAtIDAuNSkKICAgIGdhaW4gPSBhbXQgKiAoZWRnZSBpZiBzaWRlID09ICJCVVkiIGVsc2UgLWVk"
        "Z2UpCiAgICBpZiBnYWluIDwgbGltaXRzLmNvc3RfbXVsdCAqIGNvc3Q6CiAgICAgICAgcmV0dXJuIEZhbHNlLCAoZiLpoJDmnJ/l"
        "hKrli6LntIQge2dhaW46LC4wZn0g5YWDIDwge2xpbWl0cy5jb3N0X211bHR9w5cg5Lqk5piT5oiQ5pysIHtjb3N0OiwuMGZ9IOWF"
        "gyIKICAgICAgICAgICAgICAgICAgICAgICBmIu+8iOipleWIhiB7c2NvcmU6LjJmfe+8iSIpCiAgICByZXR1cm4gVHJ1ZSwgIiIK"
    ),
}


class PatchError(Exception):
    pass


# ----------------------------------------------------------------------------- helpers
def replace_once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise PatchError(f"{label}: expected exactly 1 occurrence of anchor, found {n}")
    return text.replace(old, new)


def sub_once(text: str, pattern: str, repl, label: str, flags=re.M) -> str:
    out, n = re.subn(pattern, repl, text, flags=flags)
    if n != 1:
        raise PatchError(f"{label}: expected exactly 1 match of pattern, found {n}")
    return out


# ----------------------------------------------------------------------------- portfolio_agent.py
def pa_import(t):
    return sub_once(t, r"^from agents import check_agent[ \t]*$", "from agents import check_agent\nimport reconcile",
                    "portfolio_agent import")


def pa_live_loop(t):
    pat = (r"^([ \t]*)decisions, orders, _, fix = repair_post_band_cash\(decisions, no_trade, state, closes, limits\)\n"
           r"[ \t]*attempt_notes \+= \(\[fix\] if fix else \[\]\) \+ \[f\"cost gate kept: \{s\}\" for s in skipped\]\n")

    def repl(m):
        i = m.group(1)
        return (f"{i}decisions, no_trade, orders, _, rec_notes = reconcile.apply(\n"
                f"{i}    decisions, no_trade, infs, mv, portfolio, exits, state, closes, limits,\n"
                f"{i}    score_map, rm, skipped)\n"
                f"{i}attempt_notes += rec_notes + [f\"cost gate kept: {{s}}\" for s in skipped]\n")
    return sub_once(t, pat, repl, "portfolio_agent live loop")


def pa_precheck(t):
    pat = r"^([ \t]*)errs \+= check_agent\.check_narrative_consistency\(infs, weights_map\)[ \t]*\n"

    def repl(m):
        i = m.group(1)
        return (m.group(0) + f"{i}if not errs:\n"
                f"{i}    errs += reconcile.precheck_plan(portfolio, exits, state, closes, limits)\n")
    return sub_once(t, pat, repl, "portfolio_agent pre-check")


def pa_fallback(t):
    pat = r"^([ \t]*)decisions, orders, report, _ = repair_post_band_cash\(decisions, no_trade, state, closes, limits\)[ \t]*\n"

    def repl(m):
        i = m.group(1)
        return (f"{i}decisions, no_trade, orders, report, rec_notes = reconcile.apply(\n"
                f"{i}    decisions, no_trade, infs, mv, portfolio, exits, state, closes, limits,\n"
                f"{i}    score_map, rm, None)\n"
                f"{i}notes += rec_notes\n")
    return sub_once(t, pat, repl, "portfolio_agent fallback")


def pa_turnover(t):
    pat = (r'^([ \t]*"""Req 3: Limits daily one-way turnover to max_turnover by scaling down discretionary trades\."""[ \t]*\n)')

    def repl(m):
        return (m.group(1) + "    if not state[\"holdings\"]:      # day 1 build has no turnover limit (check_turnover also skips it)\n"
                "        return decisions, None\n")
    return sub_once(t, pat, repl, "portfolio_agent turnover day-1 guard")


def pa_cash_status(t):
    pat = r"^([ \t]*)\"baseline_portfolio\": base_w,[ \t]*\n"

    def repl(m):
        i = m.group(1)
        return (f'{i}"cash_status": None if not limits else {{\n'
                f'{i}    "current_cash_ratio": round(state["cash"] / nav, 4),\n'
                f'{i}    "window": [limits.cash_lo, limits.cash_hi],\n'
                f'{i}    "must_net_sell_pct_nav": round(max(0.0, limits.cash_lo + 0.003 - state["cash"] / nav), 4),\n'
                f'{i}    "max_net_buy_pct_nav": round(max(0.0, state["cash"] / nav - limits.cash_lo - 0.003), 4)}},\n'
                + m.group(0))
    return sub_once(t, pat, repl, "portfolio_agent cash_status")


def pa_gate_rows(t):
    pat = r"^([ \t]*)tk = tech_map\.get\(t\)[ \t]*\n"

    def repl(m):
        i = m.group(1)
        return (f'{i}sc_ = _num(f.get("score"))\n'
                f'{i}if limits is not None:\n'
                f'{i}    row["gate"] = {{"buy_ok": sc_ is not None and sc_ >= limits.hurdle_buy,\n'
                f'{i}                   "sell_ok": sc_ is not None and sc_ <= limits.hurdle_sell,\n'
                f'{i}                   "days_held": state.get("days_held", {{}}).get(t, 0)}}\n'
                + m.group(0))
    return sub_once(t, pat, repl, "portfolio_agent candidate gate")


# ----------------------------------------------------------------------------- check_agent.py
def ca_import(t):
    return sub_once(t, r"^from derive_orders import derive_orders, project_portfolio[ \t]*$",
                    "from derive_orders import derive_orders, project_portfolio\n"
                    "from direction_checks import check_direction, check_narrative_vs_orders",
                    "check_agent import")


def ca_final_gate(t):
    pat = r"^([ \t]*)errs \+= _chain\(d\) \+ _coverage\(d, state, universe\) \+ _times\(d\)[ \t]*\n"

    def repl(m):
        i = m.group(1)
        return (m.group(0) + f"{i}errs += check_direction(d)\n{i}warns += check_narrative_vs_orders(d, state)\n")
    return sub_once(t, pat, repl, "check_agent final_gate")


def ca_regex(t):
    return replace_once(t, "出清|減碼)", "出清)", "check_agent exclusion regex (減碼)")


# ----------------------------------------------------------------------------- run_daily.py
def rd_tracker(t):
    pat = (r'^([ \t]*)tracker_file = ROOT / "logs" / "days_held\.json"\n.*?'
           r'tracker_file\.write_text\(json\.dumps\(current_days_held, indent=2\), encoding="utf-8"\)[ \t]*\n')

    def repl(m):
        i = m.group(1)
        body = (
            'tracker_file = ROOT / "logs" / "days_held.json"\n'
            'trk = {}\n'
            'if tracker_file.exists():\n'
            '    try:\n'
            '        trk = json.loads(tracker_file.read_text(encoding="utf-8"))\n'
            '    except Exception:\n'
            '        trk = {}\n'
            '# count each trade date once: re-running the same date must not add another day\n'
            'base = trk.get("prev_days_held", {}) if trk.get("last_trade_date") == d else trk.get("days_held", {})\n'
            'current_days_held = {t: base.get(t, 0) + 1 for t in state["holdings"]}\n'
            'state["days_held"] = current_days_held\n'
            'if not a.replay:\n'
            '    (ROOT / "logs").mkdir(exist_ok=True)\n'
            '    tracker_file.write_text(json.dumps({"last_trade_date": d, "days_held": current_days_held,\n'
            '                                        "prev_days_held": base}, indent=2), encoding="utf-8")\n')
        return "".join(i + ln if ln.strip() else ln for ln in body.splitlines(True))
    return sub_once(t, pat, repl, "run_daily days_held tracker", flags=re.M | re.S)


# ----------------------------------------------------------------------------- system_prompt.md
PROMPT_ADD = (
    "- CASH STATUS: `cash_status` tells you how much you may net-buy (`max_net_buy_pct_nav`) or must net-sell\n"
    "  (`must_net_sell_pct_nav`). In risk_off, or when net_exposure_intent is \"reduce\", never ADD/BUY unless the same plan\n"
    "  funds it with explicit trims. An ADD that pushes cash below the window is cancelled by code.\n"
    "- GATES: per candidate, `gate.buy_ok=false` means an ADD/BUY will be cancelled by the cost gate; `gate.sell_ok=false`\n"
    "  or `days_held<5` means an exit/trim of a held stock will be cancelled (unless VETO). Do not write inferences that\n"
    "  promise a trade the gate will cancel; keep the stock in `portfolio` instead.\n")


def sp_rules(t):
    anchor = "- Every holding and every exit cites valid inference ids; every inference cites valid observation ids.\n"
    return replace_once(t, anchor, anchor + PROMPT_ADD, "system_prompt hard rules")


# ----------------------------------------------------------------------------- patch table
# (relative file, name, marker proving it is already applied, function)
PATCHES = [
    ("src/agents/portfolio_agent.py", "import reconcile", "import reconcile", pa_import),
    ("src/agents/portfolio_agent.py", "reconcile step (live loop)", "score_map, rm, skipped)", pa_live_loop),
    ("src/agents/portfolio_agent.py", "plan pre-check", "reconcile.precheck_plan", pa_precheck),
    ("src/agents/portfolio_agent.py", "reconcile step (fallback)", "score_map, rm, None)", pa_fallback),
    ("src/agents/portfolio_agent.py", "turnover day-1 guard", "day 1 build has no turnover limit", pa_turnover),
    ("src/agents/portfolio_agent.py", "cash_status in prompt", '"cash_status"', pa_cash_status),
    ("src/agents/portfolio_agent.py", "gate info per candidate", 'row["gate"]', pa_gate_rows),
    ("src/agents/check_agent.py", "import direction checks", "from direction_checks import", ca_import),
    ("src/agents/check_agent.py", "final_gate direction check", "errs += check_direction(d)", ca_final_gate),
    ("src/agents/check_agent.py", "drop 減碼 from exclusion regex", None, ca_regex),
    ("src/run_daily.py", "days_held once per trade date", "last_trade_date", rd_tracker),
    ("src/prompts/system_prompt.md", "cash/gate rules", "- CASH STATUS:", sp_rules),
]
NEW_FILES = ["src/reconcile.py", "src/direction_checks.py"]
REPLACED = ["src/derive_orders.py"]


# ----------------------------------------------------------------------------- IO
def read(path: Path):
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), eol


def write(path: Path, text: str, eol: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", eol).encode("utf-8"))


def find_repo(arg: str) -> Path:
    p = Path(arg).resolve()
    for cand in (p, p / "agent-fund"):
        if (cand / "src" / "run_daily.py").exists():
            return cand
    sys.exit(f"ERROR: {p} does not look like the agent-fund repo (src/run_daily.py not found)")


def plan(repo: Path):
    """Compute every new file content in memory. Returns (changes, report)."""
    changes, report = {}, []
    texts, eols = {}, {}
    for rel, name, marker, fn in PATCHES:
        path = repo / rel
        if not path.exists():
            raise PatchError(f"{rel}: file not found")
        if rel not in texts:
            texts[rel], eols[rel] = read(path)
        t = texts[rel]
        if marker and marker in t:
            report.append(("skip", rel, name, "already applied"))
            continue
        if marker is None and "出清|減碼)" not in t:
            report.append(("skip", rel, name, "already applied"))
            continue
        texts[rel] = fn(t)
        report.append(("patch", rel, name, "ok"))
    for rel, t in texts.items():
        if t != read(repo / rel)[0]:
            changes[rel] = (t, eols[rel])
    for rel in NEW_FILES + REPLACED:
        new = base64.b64decode(EMBEDDED[rel]).decode("utf-8")
        path = repo / rel
        if path.exists() and read(path)[0] == new:
            report.append(("skip", rel, "file", "identical"))
            continue
        eol = read(path)[1] if path.exists() else "\n"
        changes[rel] = (new, eol)
        report.append(("replace" if path.exists() else "create", rel, "file", "ok"))
    return changes, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".", help="path to the agent-fund repo (default: current dir)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    repo = find_repo(a.repo)
    bdir = repo / ".patch_backup"

    if a.revert:
        runs = sorted(p for p in bdir.glob("*") if p.is_dir()) if bdir.exists() else []
        if not runs:
            sys.exit("nothing to revert: no .patch_backup found")
        last = runs[-1]
        manifest = json.loads((last / "manifest.json").read_text(encoding="utf-8"))
        for rel in manifest["existing"]:
            shutil.copy2(last / rel, repo / rel)
            print(f"restored {rel}")
        for rel in manifest["created"]:
            (repo / rel).unlink(missing_ok=True)
            print(f"removed  {rel}")
        shutil.rmtree(last)
        print(f"reverted backup {last.name}")
        return

    try:
        changes, report = plan(repo)
    except PatchError as e:
        sys.exit(f"ABORTED, nothing written: {e}\n(the repo differs from the version this patch was written for; "
                 f"apply the edits in PATCHES.md by hand for that spot)")
    for kind, rel, name, note in report:
        print(f"  [{kind:7}] {rel}  -  {name} ({note})")
    if not changes:
        print("\nNothing to do: everything is already applied.")
        return
    if a.dry_run:
        print(f"\nDRY RUN: {len(changes)} file(s) would be written. Nothing changed.")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bk = bdir / stamp
    existing = [r for r in changes if (repo / r).exists()]
    created = [r for r in changes if r not in existing]
    for rel in existing:
        (bk / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, bk / rel)
    bk.mkdir(parents=True, exist_ok=True)
    (bk / "manifest.json").write_text(json.dumps({"existing": existing, "created": created}, indent=1), encoding="utf-8")

    def rollback():
        for rel in existing:
            shutil.copy2(bk / rel, repo / rel)
        for rel in created:
            (repo / rel).unlink(missing_ok=True)

    try:
        for rel, (text, eol) in changes.items():
            write(repo / rel, text, eol)
        for rel in changes:
            if rel.endswith(".py"):
                with tempfile.TemporaryDirectory() as td:
                    py_compile.compile(str(repo / rel), cfile=str(Path(td) / "x.pyc"), doraise=True)
    except Exception as e:
        rollback()
        sys.exit(f"ERROR after writing ({type(e).__name__}: {e}); everything rolled back from backup.")
    print(f"\nDONE: {len(changes)} file(s) written. Backup: {bk}")
    print("Next: python -c \"import sys; sys.path.insert(0,'src'); import reconcile, direction_checks\"  (sanity import)\n"
          "      then a dry run:  python src/run_daily.py <date> --replay logs/<date>_attemptN.txt --force")


if __name__ == "__main__":
    main()