#!/usr/bin/env python3
"""Official Block plugin: block-image-host (Cloudflare R2 / S3 / GitHub image hosting)."""

import base64
import datetime
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = {
    "provider": "r2",
    "s3_endpoint": "",
    "s3_bucket": "",
    "s3_access_key": "",
    "s3_secret_key": "",
    "public_domain": "",
    "gh_repo": "",
    "gh_branch": "main",
    "gh_token": "",
    "gh_cdn": "jsdelivr",
    "remote_prefix": "",
    "auto_upload": True,
    "naming_rule": "ym_hash",
    "selected_notebooks": [],
}

PROVIDER_LABELS = {
    "r2": "Cloudflare R2",
    "s3": "S3 兼容",
    "github": "GitHub 仓库",
}

VALID_PROVIDERS = {"r2", "s3", "github"}
VALID_GH_CDNS = {"jsdelivr", "raw"}
VALID_NAMING_RULES = {"ym_hash", "note_dir", "original"}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    ".avif",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
}

MAX_LOGS = 20
MAX_PENDING_DISPLAY = 6
MAX_RECENT_LOGS = 5

FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE_PATTERN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1")
MD_IMAGE_PATTERN = re.compile(
    r"!\[((?:\\.|[^\]\n])*)\]\("
    r"(<[^>\n]+>(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?"
    r"|[^()\s\n]*(?:\([^()\s\n]*\)[^()\s\n]*)*(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?)\)"
)
HTML_IMG_TAG_PATTERN = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
HTML_SRC_ATTR_PATTERN = re.compile(
    r"""(\bsrc\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


class HostBridge:
    def __init__(self):
        self._seq = 1000

    def send(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def call_host(self, method, params=None):
        self._seq += 1
        req_id = f"p-{self._seq}"
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("stdin closed while awaiting host response")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if str(msg.get("id")) == req_id:
                if "error" in msg and msg["error"]:
                    raise RuntimeError(msg["error"].get("message", "host error"))
                return msg.get("result", {})

    def serve(self, handler):
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            try:
                result = handler(self, method, req.get("params") or {})
                self.send({"jsonrpc": "2.0", "id": req_id, "result": result})
                if method == "plugin.shutdown":
                    break
            except Exception as exc:
                self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}})


class ImageHostPlugin:
    def __init__(self):
        self.notebook_root = ""
        self.known_notebooks = []
        self.data_dir = ""
        self.locale = "zh"
        self.supports_checklist = False
        self.supports_multi_notebook = False

    # ---- Notebook helpers ----

    def _norm_nb_path(self, raw):
        text = str(raw or "").strip()
        if not text:
            return ""
        return os.path.normpath(os.path.abspath(os.path.expanduser(text)))

    def _display_path(self, raw):
        path = self._norm_nb_path(raw)
        if not path:
            return ""
        home = os.path.normpath(os.path.expanduser("~"))
        if path == home:
            return "~"
        if path.startswith(home + os.sep):
            return "~" + path[len(home) :]
        return path

    def _notebook_label(self, nb_root):
        norm = self._norm_nb_path(nb_root)
        for entry in self.known_notebooks:
            if entry["path"] == norm and entry.get("name"):
                return entry["name"]
        return os.path.basename(norm) or norm or "默认笔记本"

    def _refresh_known_notebooks(self, bridge):
        entries = []
        seen = set()

        def add_entry(path_raw, name_raw="", active=False):
            norm = self._norm_nb_path(path_raw)
            if not norm or norm in seen:
                return
            seen.add(norm)
            label = str(name_raw or "").strip() or os.path.basename(norm) or norm
            entries.append({"path": norm, "name": label, "active": bool(active)})

        active_norm = self._norm_nb_path(self.notebook_root)
        fetched_from_host = False
        if bridge is not None:
            try:
                res = bridge.call_host("host.notebooks.list", {})
                raw_list = res.get("notebooks")
                if isinstance(raw_list, list):
                    fetched_from_host = True
                    for item in raw_list:
                        if isinstance(item, dict):
                            p = item.get("path")
                            is_active = bool(item.get("active")) or (
                                bool(active_norm) and self._norm_nb_path(p) == active_norm
                            )
                            if is_active and p:
                                active_norm = self._norm_nb_path(p)
                                self.notebook_root = active_norm
                            add_entry(p, item.get("name"), is_active)
            except Exception:
                pass

        if not fetched_from_host:
            for existing in self.known_notebooks:
                if isinstance(existing, dict):
                    p = existing.get("path")
                    add_entry(p, existing.get("name"), bool(active_norm) and self._norm_nb_path(p) == active_norm)
                elif isinstance(existing, str):
                    add_entry(existing, "", bool(active_norm) and self._norm_nb_path(existing) == active_norm)

        if active_norm:
            add_entry(active_norm, os.path.basename(active_norm) or active_norm, True)

        self.known_notebooks = entries
        return entries

    def _selected_notebooks(self, config):
        raw = config.get("selected_notebooks")
        known_paths = [e["path"] for e in self.known_notebooks]
        known_set = set(known_paths)
        out = []
        seen = set()
        if isinstance(raw, list):
            for item in raw:
                norm = self._norm_nb_path(item)
                if not norm or norm in seen:
                    continue
                if known_set and norm not in known_set:
                    continue
                seen.add(norm)
                out.append(norm)
        return out

    # ---- Storage helpers ----

    def _load_state(self, bridge):
        self._refresh_known_notebooks(bridge)
        try:
            res = bridge.call_host("host.storage.get", {})
            data = res.get("data") or {}
        except Exception:
            data = {}
        raw_cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
        config = self._normalize_config(raw_cfg)
        if "selected_notebooks" not in raw_cfg:
            active_norm = self._norm_nb_path(self.notebook_root)
            if active_norm:
                config["selected_notebooks"] = [active_norm]
            elif self.known_notebooks:
                config["selected_notebooks"] = [self.known_notebooks[0]["path"]]

        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        uploaded_total = int(stats.get("uploaded_total", 0) or 0)
        logs = data.get("logs") if isinstance(data.get("logs"), list) else []
        clean_logs = []
        for entry in logs[:MAX_LOGS]:
            if isinstance(entry, dict) and entry.get("title") and entry.get("time"):
                item = {"title": str(entry["title"]), "time": str(entry["time"])}
                if entry.get("status"):
                    item["status"] = str(entry["status"])
                if entry.get("tone"):
                    item["tone"] = str(entry["tone"])
                if entry.get("tooltip"):
                    item["tooltip"] = str(entry["tooltip"])
                clean_logs.append(item)
        return config, {"uploaded_total": uploaded_total}, clean_logs

    def _save_config(self, bridge, config):
        bridge.call_host("host.storage.set", {"key": "config", "value": config})

    def _save_stats(self, bridge, stats):
        bridge.call_host("host.storage.set", {"key": "stats", "value": stats})

    def _save_logs(self, bridge, logs):
        bridge.call_host("host.storage.set", {"key": "logs", "value": logs[:MAX_LOGS]})

    def _append_log(self, logs, title, status_text, tone="info", tooltip=""):
        now_str = datetime.datetime.now().strftime("%H:%M")
        entry = {"title": title, "time": now_str, "tone": tone}
        if status_text:
            entry["status"] = status_text
        if tooltip:
            entry["tooltip"] = tooltip
        return [entry] + logs[: MAX_LOGS - 1]

    def _normalize_config(self, raw):
        cfg = dict(DEFAULT_CONFIG)
        cfg["selected_notebooks"] = []
        if not isinstance(raw, dict):
            return cfg
        provider = str(raw.get("provider", cfg["provider"])).strip()
        if provider in VALID_PROVIDERS:
            cfg["provider"] = provider

        str_fields = (
            "s3_endpoint",
            "s3_bucket",
            "s3_access_key",
            "s3_secret_key",
            "public_domain",
            "gh_repo",
            "gh_branch",
            "gh_token",
            "remote_prefix",
        )
        for key in str_fields:
            if key in raw and raw[key] is not None:
                cfg[key] = str(raw[key]).strip()

        # Backward compatibility with legacy config keys
        legacy_endpoint = str(raw.get("endpoint") or "").strip()
        if legacy_endpoint:
            if cfg["provider"] in ("r2", "s3") and not cfg["s3_endpoint"]:
                cfg["s3_endpoint"] = legacy_endpoint
            elif cfg["provider"] == "github" and not cfg["gh_repo"]:
                cfg["gh_repo"] = legacy_endpoint

        legacy_bucket = str(raw.get("bucket_prefix") or "").strip()
        if legacy_bucket and cfg["provider"] in ("r2", "s3") and not cfg["s3_bucket"]:
            cfg["s3_bucket"] = legacy_bucket

        legacy_secret = str(raw.get("access_secret") or "").strip()
        if legacy_secret:
            if cfg["provider"] in ("r2", "s3") and not cfg["s3_access_key"] and not cfg["s3_secret_key"]:
                if ":" in legacy_secret:
                    ak, sk = legacy_secret.split(":", 1)
                    cfg["s3_access_key"] = ak.strip()
                    cfg["s3_secret_key"] = sk.strip()
            elif cfg["provider"] == "github" and not cfg["gh_token"]:
                cfg["gh_token"] = legacy_secret

        legacy_branch = raw.get("gh_branch_preset")
        if legacy_branch in ("main", "master"):
            cfg["gh_branch"] = legacy_branch

        if "remote_prefix" not in raw:
            legacy_prefix = raw.get("prefix_preset")
            if legacy_prefix in ("images", "block"):
                cfg["remote_prefix"] = legacy_prefix
            elif legacy_prefix == "custom":
                cfg["remote_prefix"] = str(raw.get("custom_prefix") or "").strip()

        gh_cdn = str(raw.get("gh_cdn", cfg["gh_cdn"])).strip()
        if gh_cdn in VALID_GH_CDNS:
            cfg["gh_cdn"] = gh_cdn

        if "auto_upload" in raw:
            cfg["auto_upload"] = bool(raw["auto_upload"])

        naming_rule = str(raw.get("naming_rule", cfg["naming_rule"])).strip()
        if naming_rule in VALID_NAMING_RULES:
            cfg["naming_rule"] = naming_rule

        if "selected_notebooks" in raw and isinstance(raw["selected_notebooks"], list):
            seen = set()
            dedup = []
            for item in raw["selected_notebooks"]:
                norm = self._norm_nb_path(item)
                if norm and norm not in seen:
                    seen.add(norm)
                    dedup.append(norm)
            cfg["selected_notebooks"] = dedup

        return cfg

    def _effective_prefix(self, config, bucket_prefix=""):
        prefix = config["remote_prefix"].strip("/")
        return "/".join(part for part in (bucket_prefix.strip("/"), prefix) if part)

    # ---- Markdown & HTML local image scanning ----

    def _parse_image_inner(self, raw_inner):
        text = raw_inner.strip()
        if not text:
            return None, ""
        if text.startswith("<"):
            end_idx = text.find(">")
            if end_idx > 1:
                target = text[1:end_idx].strip()
                suffix = text[end_idx + 1 :]
                return target, suffix
        parts = text.split(None, 1)
        target = parts[0]
        suffix = (" " + parts[1]) if len(parts) > 1 else ""
        return target, suffix

    def _normalize_local_asset_ref(self, target):
        if not target:
            return None
        stripped = target.strip()
        lower = stripped.lower()
        if lower.startswith(("http://", "https://", "data:", "file:", "mailto:", "blob:", "//")):
            return None
        clean = stripped.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean or clean.startswith("/"):
            return None
        if len(clean) >= 2 and clean[1] == ":" and clean[0].isalpha():
            return None
        unquoted = urllib.parse.unquote(clean)
        _, ext = os.path.splitext(unquoted.lower())
        if ext not in IMAGE_EXTENSIONS:
            return None
        return clean

    def _mask_inline_code(self, line):
        """Replace inline code spans with same-length spaces to preserve character offsets."""
        return INLINE_CODE_PATTERN.sub(lambda m: " " * (m.end() - m.start()), line)

    def _extract_local_refs(self, content):
        """Return list of local image ref dicts outside fenced & inline code blocks.

        Each dict has:
          - kind: "md" | "html"
          - start, end: replacement span in `content`
          - rel_path: relative local asset path (e.g. ./assets/foo.png)
          - alt, suffix (for "md" only)
        """
        refs = []
        fence_char = ""
        fence_len = 0
        offset = 0

        for line in content.splitlines(keepends=True):
            fence_match = FENCE_PATTERN.match(line)
            if fence_match:
                marker = fence_match.group(1)
                if not fence_char:
                    fence_char = marker[0]
                    fence_len = len(marker)
                    offset += len(line)
                    continue
                if marker[0] == fence_char and len(marker) >= fence_len and line[fence_match.end() :].strip() == "":
                    fence_char = ""
                    fence_len = 0
                    offset += len(line)
                    continue

            if not fence_char:
                scan_line = self._mask_inline_code(line)
                line_refs = []

                for match in MD_IMAGE_PATTERN.finditer(scan_line):
                    orig_alt = line[match.start(1) : match.end(1)]
                    orig_inner = line[match.start(2) : match.end(2)]
                    target, suffix = self._parse_image_inner(orig_inner)
                    rel_path = self._normalize_local_asset_ref(target)
                    if rel_path:
                        line_refs.append(
                            {
                                "kind": "md",
                                "start": offset + match.start(),
                                "end": offset + match.end(),
                                "alt": orig_alt,
                                "rel_path": rel_path,
                                "suffix": suffix,
                            }
                        )

                for tag_match in HTML_IMG_TAG_PATTERN.finditer(scan_line):
                    tag_start = tag_match.start()
                    orig_tag = line[tag_start : tag_match.end()]
                    src_match = HTML_SRC_ATTR_PATTERN.search(orig_tag)
                    if not src_match:
                        continue
                    val_group = 2 if src_match.group(2) is not None else (3 if src_match.group(3) is not None else 4)
                    raw_src = src_match.group(val_group)
                    unescaped_src = html.unescape(raw_src)
                    rel_path = self._normalize_local_asset_ref(unescaped_src)
                    if rel_path:
                        line_refs.append(
                            {
                                "kind": "html",
                                "start": offset + tag_start + src_match.start(val_group),
                                "end": offset + tag_start + src_match.end(val_group),
                                "rel_path": rel_path,
                            }
                        )

                line_refs.sort(key=lambda item: (item["start"], item["end"]))
                last_end = -1
                for item in line_refs:
                    if item["start"] >= last_end:
                        refs.append(item)
                        last_end = item["end"]

            offset += len(line)
        return refs

    def _resolve_local_file(self, nb_root, note_path, rel_asset_path):
        if not nb_root:
            return None
        root_real = os.path.realpath(nb_root)
        unquoted = urllib.parse.unquote(rel_asset_path)
        note_dir = os.path.dirname(note_path) if note_path else ""
        candidates = []
        if note_dir:
            candidates.append(os.path.join(root_real, note_dir, unquoted))
            candidates.append(os.path.join(root_real, note_dir, rel_asset_path))
        candidates.append(os.path.join(root_real, unquoted))
        candidates.append(os.path.join(root_real, rel_asset_path))
        for cand in candidates:
            real_cand = os.path.realpath(cand)
            if real_cand == root_real or real_cand.startswith(root_real + os.sep):
                if os.path.isfile(real_cand):
                    return real_cand
        return None

    def _scan_notebooks(self, bridge, notebooks, target_note=None):
        """Scan specified notebooks and return (scanned_notes, total_refs, note_items).

        Each item in note_items is (nb_root, nb_name, note_path, content, refs).
        `target_note`, if given, is (target_nb_root, target_rel_path).
        """
        results = []
        total_refs = 0
        scanned_notes = 0

        target_nb = self._norm_nb_path(target_note[0]) if target_note else ""
        target_rel = str(target_note[1]).strip() if target_note else ""

        for nb_root in notebooks:
            norm_nb = self._norm_nb_path(nb_root)
            if not norm_nb:
                continue
            if target_nb and norm_nb != target_nb:
                continue
            nb_name = self._notebook_label(norm_nb)

            if target_rel:
                note_paths = [target_rel]
            else:
                try:
                    list_res = bridge.call_host("host.notes.list", {"notebook_root": norm_nb})
                    notes = list_res.get("notes") or []
                except Exception:
                    continue
                note_paths = [
                    item.get("path")
                    for item in notes
                    if isinstance(item, dict) and item.get("path")
                ]

            for note_path in note_paths:
                scanned_notes += 1
                try:
                    read_res = bridge.call_host(
                        "host.notes.read",
                        {"notebook_root": norm_nb, "path": note_path},
                    )
                    content = read_res.get("content", "")
                except Exception:
                    continue
                raw_refs = self._extract_local_refs(content)
                refs = [
                    ref
                    for ref in raw_refs
                    if self._resolve_local_file(norm_nb, note_path, ref["rel_path"]) is not None
                ]
                if refs:
                    total_refs += len(refs)
                    results.append((norm_nb, nb_name, note_path, content, refs))

        return scanned_notes, total_refs, results

    # ---- Config parsing & validation ----

    def _detect_s3_region(self, provider, netloc):
        if provider == "r2":
            return "auto"
        patterns = (
            r"s3[.-]([a-z0-9-]+)\.amazonaws\.com",
            r"s3\.([a-z0-9-]+)\.backblazeb2\.com",
            r"([a-z0-9-]+)\.digitaloceanspaces\.com",
            r"oss-([a-z0-9-]+)\.aliyuncs\.com",
            r"cos\.([a-z0-9-]+)\.myqcloud\.com",
        )
        for pat in patterns:
            m = re.search(pat, netloc, re.IGNORECASE)
            if m:
                return m.group(1).lower()
        return "us-east-1"

    def _parse_s3_credentials(self, config):
        endpoint = config.get("s3_endpoint", "").strip()
        if not endpoint:
            return None, "请填写 Endpoint 地址"
        if not endpoint.startswith(("http://", "https://")):
            endpoint = "https://" + endpoint
        parsed = urllib.parse.urlparse(endpoint)
        if not parsed.netloc:
            return None, "Endpoint 地址格式无效"

        bp = config.get("s3_bucket", "").strip().strip("/")
        if not bp:
            return None, "请填写 Bucket 存储桶名"
        bp_parts = bp.split("/", 1)
        bucket = bp_parts[0].strip()
        bucket_prefix = bp_parts[1].strip("/") if len(bp_parts) > 1 else ""
        if not bucket:
            return None, "Bucket 存储桶名不能为空"

        # Avoid duplicate /<bucket>/<bucket> when user pastes endpoint including bucket path
        if parsed.path.strip("/") == bucket:
            endpoint = f"{parsed.scheme}://{parsed.netloc}"
            parsed = urllib.parse.urlparse(endpoint)

        prefix = self._effective_prefix(config, bucket_prefix)

        access_key = config.get("s3_access_key", "").strip()
        secret_key = config.get("s3_secret_key", "").strip()
        if not access_key or not secret_key:
            return None, "请完整填写 Access Key ID 与 Secret Access Key"

        public_domain = config.get("public_domain", "").strip().rstrip("/")
        if public_domain and not public_domain.startswith(("http://", "https://")):
            public_domain = "https://" + public_domain
        if config["provider"] == "r2" and not public_domain:
            return None, "Cloudflare R2 需填写公开访问域名（如 https://pub-xxx.r2.dev），否则图片无法公开加载"

        region = self._detect_s3_region(config["provider"], parsed.netloc)

        return {
            "endpoint": endpoint.rstrip("/"),
            "parsed_endpoint": parsed,
            "bucket": bucket,
            "prefix": prefix,
            "public_domain": public_domain,
            "access_key": access_key,
            "secret_key": secret_key,
            "region": region,
        }, None

    def _parse_github_repo(self, repo_spec):
        spec = repo_spec.strip()
        if spec.startswith(("http://", "https://")):
            parsed = urllib.parse.urlparse(spec)
            spec = parsed.path.strip("/")
            if spec.endswith(".git"):
                spec = spec[:-4]
        branch_hint = ""
        if "@" in spec:
            spec, branch_part = spec.split("@", 1)
            branch_hint = branch_part.strip()
        parts = [p for p in spec.split("/") if p]
        if len(parts) != 2:
            return None, None, branch_hint
        return parts[0], parts[1], branch_hint

    def _parse_github_credentials(self, config):
        repo_spec = config.get("gh_repo", "").strip()
        if not repo_spec:
            return None, "请填写 GitHub 仓库（格式: owner/repo）"
        owner, repo, branch_hint = self._parse_github_repo(repo_spec)
        if not owner or not repo:
            return None, "GitHub 仓库格式应为 owner/repo"

        branch = config["gh_branch"] or branch_hint or "main"

        token = config.get("gh_token", "").strip()
        if not token:
            return None, "请填写 GitHub Personal Access Token"

        gh_cdn = config.get("gh_cdn", "jsdelivr")
        if gh_cdn not in VALID_GH_CDNS:
            gh_cdn = "jsdelivr"

        return {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "prefix": self._effective_prefix(config),
            "token": token,
            "gh_cdn": gh_cdn,
        }, None

    # ---- Network upload & connection test ----

    def _extract_http_error_detail(self, err):
        detail = f"HTTP {err.code}"
        try:
            raw = err.read(4096)
            if not raw:
                return detail
            text = raw.decode("utf-8", errors="replace").strip()
            if text.startswith("{"):
                data = json.loads(text)
                msg = str(data.get("message") or "").strip()
                if msg:
                    return f"HTTP {err.code}: {msg}"
            code_m = re.search(r"<Code>([^<]+)</Code>", text)
            msg_m = re.search(r"<Message>([^<]+)</Message>", text)
            if code_m and msg_m:
                return f"HTTP {err.code} ({code_m.group(1)}): {msg_m.group(1)}"
            if code_m:
                return f"HTTP {err.code} ({code_m.group(1)})"
        except Exception:
            pass
        return detail

    def _s3_signed_request_once(self, creds, method, path_segments, query_params=None, body=b"", content_type=None):
        parsed = creds["parsed_endpoint"]
        base_path = parsed.path.rstrip("/")
        quoted_segs = [urllib.parse.quote(seg, safe="-_.~") for seg in path_segments if seg != ""]
        canonical_uri = f"{base_path}/" + "/".join(quoted_segs)
        if not canonical_uri.startswith("/"):
            canonical_uri = "/" + canonical_uri

        query_items = sorted((k, v) for k, v in (query_params or {}).items())
        canonical_qs = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
            for k, v in query_items
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        host_header = parsed.netloc

        headers_to_sign = {
            "host": host_header,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        sorted_header_keys = sorted(headers_to_sign.keys())
        canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted_header_keys)
        signed_headers = ";".join(sorted_header_keys)

        canonical_request = (
            f"{method}\n"
            f"{canonical_uri}\n"
            f"{canonical_qs}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )
        credential_scope = f"{date_stamp}/{creds['region']}/s3/aws4_request"
        string_to_sign = (
            "AWS4-HMAC-SHA256\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        k_date = hmac.new(
            ("AWS4" + creds["secret_key"]).encode("utf-8"),
            date_stamp.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        k_region = hmac.new(k_date, creds["region"].encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        url = f"{parsed.scheme}://{parsed.netloc}{canonical_uri}"
        if canonical_qs:
            url += f"?{canonical_qs}"

        req_headers = {
            "Host": host_header,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": auth_header,
        }
        if content_type:
            req_headers["Content-Type"] = content_type
        if method == "PUT":
            req_headers["Content-Length"] = str(len(body))

        req = urllib.request.Request(url, data=body if method == "PUT" else None, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()

    def _s3_signed_request(self, creds, method, path_segments, query_params=None, body=b"", content_type=None):
        try:
            return self._s3_signed_request_once(
                creds, method, path_segments, query_params=query_params, body=body, content_type=content_type
            )
        except urllib.error.HTTPError as err:
            if err.code in (301, 400, 403):
                hint_region = (err.headers.get("x-amz-bucket-region") or "").strip() if err.headers else ""
                if not hint_region:
                    try:
                        body_preview = err.read(2048).decode("utf-8", errors="replace")
                        m = re.search(r"<Region>([^<]+)</Region>", body_preview)
                        if m:
                            hint_region = m.group(1).strip()
                    except Exception:
                        pass
                if hint_region and hint_region != creds["region"]:
                    creds["region"] = hint_region
                    return self._s3_signed_request_once(
                        creds, method, path_segments, query_params=query_params, body=body, content_type=content_type
                    )
            raise

    def _github_api_request(self, creds, method, api_url, payload=None):
        headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "block-image-host/1.2",
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})

    def _build_remote_key(self, naming_rule, prefix, nb_name, multi_nb, note_path, file_bytes, rel_asset_path):
        filename = os.path.basename(urllib.parse.unquote(rel_asset_path)) or "image.png"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)

        if naming_rule == "note_dir":
            stem = note_path.replace("\\", "/")
            if stem.endswith(".md"):
                stem = stem[:-3]
            parts = ([nb_name] if multi_nb and nb_name else []) + stem.split("/")
            safe_parts = [
                re.sub(r"[^A-Za-z0-9._-]", "_", part)
                for part in parts
                if part and part not in (".", "..")
            ]
            folder = "/".join(p for p in safe_parts if p) or "notes"
            object_key = f"{folder}/{safe_name}"
        elif naming_rule == "original":
            object_key = safe_name
        else:
            now_ym = datetime.datetime.now().strftime("%Y/%m")
            digest = hashlib.sha256(file_bytes).hexdigest()[:8]
            object_key = f"{now_ym}/{digest}-{safe_name}"

        if prefix:
            return f"{prefix}/{object_key}"
        return object_key

    def _upload_file(self, config, creds, nb_name, multi_nb, note_path, local_path, rel_asset_path):
        with open(local_path, "rb") as fh:
            file_bytes = fh.read()
        remote_key = self._build_remote_key(
            config.get("naming_rule", "ym_hash"),
            creds.get("prefix", ""),
            nb_name,
            multi_nb,
            note_path,
            file_bytes,
            rel_asset_path,
        )
        content_type, _ = mimetypes.guess_type(local_path)
        content_type = content_type or "application/octet-stream"

        if config["provider"] in ("r2", "s3"):
            segments = [creds["bucket"]] + remote_key.split("/")
            self._s3_signed_request(
                creds,
                "PUT",
                segments,
                body=file_bytes,
                content_type=content_type,
            )
            encoded_key = "/".join(urllib.parse.quote(seg, safe="-_.~") for seg in remote_key.split("/"))
            if creds["public_domain"]:
                return f"{creds['public_domain']}/{encoded_key}"
            return f"{creds['endpoint']}/{urllib.parse.quote(creds['bucket'], safe='-_.~')}/{encoded_key}"

        encoded_path = "/".join(urllib.parse.quote(seg, safe="-_.~") for seg in remote_key.split("/"))
        api_url = f"https://api.github.com/repos/{creds['owner']}/{creds['repo']}/contents/{encoded_path}"
        existing_sha = None
        try:
            _, info = self._github_api_request(
                creds,
                "GET",
                f"{api_url}?ref={urllib.parse.quote(creds['branch'], safe='')}",
            )
            if isinstance(info, dict) and info.get("sha"):
                existing_sha = info["sha"]
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise

        payload = {
            "message": f"chore(image-host): upload {os.path.basename(local_path)}",
            "content": base64.b64encode(file_bytes).decode("ascii"),
            "branch": creds["branch"],
        }
        if existing_sha:
            payload["sha"] = existing_sha

        self._github_api_request(creds, "PUT", api_url, payload=payload)
        branch_quoted = urllib.parse.quote(creds["branch"], safe="")
        if creds["gh_cdn"] == "jsdelivr":
            return f"https://cdn.jsdelivr.net/gh/{creds['owner']}/{creds['repo']}@{branch_quoted}/{encoded_path}"
        return f"https://raw.githubusercontent.com/{creds['owner']}/{creds['repo']}/{branch_quoted}/{encoded_path}"

    def _test_connection(self, config):
        if config["provider"] in ("r2", "s3"):
            creds, err_msg = self._parse_s3_credentials(config)
            if err_msg:
                return False, err_msg
            try:
                self._s3_signed_request(
                    creds,
                    "GET",
                    [creds["bucket"]],
                    query_params={"list-type": "2", "max-keys": "1"},
                )
                return True, f"{PROVIDER_LABELS[config['provider']]} 连接成功（存储桶 {creds['bucket']} 可访问）"
            except urllib.error.HTTPError as err:
                detail = self._extract_http_error_detail(err)
                return False, f"图床服务端返回 {detail}，请核对凭证、区域或存储桶权限"
            except urllib.error.URLError as err:
                return False, f"无法连接图床服务（{err.reason}）"
            except Exception as err:
                return False, f"连接校验失败：{err}"

        creds, err_msg = self._parse_github_credentials(config)
        if err_msg:
            return False, err_msg
        try:
            api_url = f"https://api.github.com/repos/{creds['owner']}/{creds['repo']}"
            _, info = self._github_api_request(creds, "GET", api_url)
            perms = info.get("permissions") if isinstance(info, dict) else {}
            if isinstance(perms, dict) and perms.get("push") is False:
                return False, f"已连接仓库 {creds['owner']}/{creds['repo']}，但当前令牌缺少写入权限"
            if isinstance(info, dict) and info.get("size", 0) > 0:
                branch_url = f"{api_url}/branches/{urllib.parse.quote(creds['branch'], safe='')}"
                try:
                    self._github_api_request(creds, "GET", branch_url)
                except urllib.error.HTTPError as b_err:
                    if b_err.code == 404:
                        default_br = info.get("default_branch") or "main"
                        return False, f"仓库 {creds['owner']}/{creds['repo']} 中不存在分支 {creds['branch']}（默认分支为 {default_br}）"
                    raise
            return True, f"GitHub 仓库 {creds['owner']}/{creds['repo']}（分支 {creds['branch']}）连接成功"
        except urllib.error.HTTPError as err:
            detail = self._extract_http_error_detail(err)
            return False, f"GitHub API 返回 {detail}，请核对仓库名称或令牌权限"
        except urllib.error.URLError as err:
            return False, f"无法连接 GitHub API（{err.reason}）"
        except Exception as err:
            return False, f"连接校验失败：{err}"

    def _format_md_replacement(self, ref, remote_url):
        dest = remote_url
        if any(ch in dest for ch in (" ", "\t", "(", ")", "<", ">")):
            dest = f"<{dest}>"
        return f"![{ref['alt']}]({dest}{ref['suffix']})"

    def _execute_upload(self, bridge, config, stats, logs, notebooks, target_note=None):
        if not notebooks:
            return stats, logs, "请先勾选至少一个笔记本", 0, 0, ""

        scanned_notes, total_refs, note_items = self._scan_notebooks(
            bridge, notebooks, target_note=target_note
        )
        if config["provider"] in ("r2", "s3"):
            creds, err_msg = self._parse_s3_credentials(config)
        else:
            creds, err_msg = self._parse_github_credentials(config)

        if err_msg:
            status = f"已扫描 {scanned_notes} 篇笔记，发现 {total_refs} 处待上传本地图片；{err_msg}"
            return stats, logs, status, 0, 0, err_msg

        if total_refs == 0:
            status = f"已扫描 {scanned_notes} 篇笔记，未发现待上传的本地图片"
            return stats, logs, status, 0, 0, ""

        uploaded_cache = {}
        uploaded_count = 0
        failed_count = 0
        updated_notes = 0
        naming_rule = config.get("naming_rule", "ym_hash")
        multi_nb = len(notebooks) > 1

        for nb_root, nb_name, note_path, content, refs in note_items:
            pieces = []
            cursor = 0
            note_changed = False
            display_note = f"{nb_name}/{note_path}" if multi_nb else note_path

            for ref in refs:
                start = ref["start"]
                end = ref["end"]
                rel_path = ref["rel_path"]
                pieces.append(content[cursor:start])
                local_path = self._resolve_local_file(nb_root, note_path, rel_path)
                if not local_path:
                    failed_count += 1
                    logs = self._append_log(logs, rel_path, "失败", tone="danger", tooltip="未找到本地文件")
                    pieces.append(content[start:end])
                    cursor = end
                    continue

                real_local = os.path.realpath(local_path)
                cache_key = (real_local, f"{nb_root}::{note_path}" if naming_rule == "note_dir" else "")
                if cache_key in uploaded_cache:
                    remote_url = uploaded_cache[cache_key]
                else:
                    try:
                        remote_url = self._upload_file(
                            config, creds, nb_name, multi_nb, note_path, local_path, rel_path
                        )
                        uploaded_cache[cache_key] = remote_url
                        uploaded_count += 1
                        stats["uploaded_total"] = int(stats.get("uploaded_total", 0)) + 1
                        logs = self._append_log(
                            logs,
                            f"{display_note} -> {os.path.basename(local_path)}",
                            "成功",
                            tone="success",
                            tooltip=remote_url,
                        )
                    except urllib.error.HTTPError as err:
                        failed_count += 1
                        detail = self._extract_http_error_detail(err)
                        logs = self._append_log(
                            logs,
                            os.path.basename(local_path),
                            "失败",
                            tone="danger",
                            tooltip=detail,
                        )
                        pieces.append(content[start:end])
                        cursor = end
                        continue
                    except urllib.error.URLError as err:
                        failed_count += 1
                        logs = self._append_log(
                            logs,
                            os.path.basename(local_path),
                            "失败",
                            tone="danger",
                            tooltip=f"网络不可达: {err.reason}",
                        )
                        pieces.append(content[start:end])
                        cursor = end
                        continue
                    except Exception as err:
                        failed_count += 1
                        logs = self._append_log(
                            logs,
                            os.path.basename(local_path),
                            "失败",
                            tone="danger",
                            tooltip=str(err),
                        )
                        pieces.append(content[start:end])
                        cursor = end
                        continue

                if ref["kind"] == "html":
                    pieces.append(html.escape(remote_url, quote=True))
                else:
                    pieces.append(self._format_md_replacement(ref, remote_url))
                note_changed = True
                cursor = end

            pieces.append(content[cursor:])
            if note_changed:
                new_content = "".join(pieces)
                try:
                    bridge.call_host(
                        "host.notes.write",
                        {"notebook_root": nb_root, "path": note_path, "content": new_content},
                    )
                    updated_notes += 1
                except Exception as err:
                    failed_count += 1
                    logs = self._append_log(
                        logs,
                        display_note,
                        "失败",
                        tone="danger",
                        tooltip=f"写回笔记失败: {err}",
                    )

        self._save_stats(bridge, stats)
        self._save_logs(bridge, logs)

        if failed_count > 0 and uploaded_count == 0:
            status = (
                f"已扫描 {scanned_notes} 篇笔记（{total_refs} 处本地图片），"
                f"本次 {failed_count} 项未能上传，请查看最近上传记录"
            )
        elif failed_count > 0:
            status = (
                f"已上传 {uploaded_count} 张图片并更新 {updated_notes} 篇笔记，"
                f"另有 {failed_count} 项失败"
            )
        else:
            status = f"成功上传 {uploaded_count} 张图片，已更新 {updated_notes} 篇笔记"
        return stats, logs, status, uploaded_count, failed_count, ""

    # ---- UI Page builder ----

    def _format_bytes(self, num_bytes):
        val = float(max(0, int(num_bytes)))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        idx = 0
        while val >= 1024.0 and idx < len(units) - 1:
            val /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(val)} B"
        if idx == 1 and (val >= 100 or abs(val - round(val)) < 0.05):
            return f"{round(val)} KiB"
        return f"{val:.1f} {units[idx]}"

    def _collect_pending_images(self, note_items, multi_nb):
        seen = {}
        ordered = []
        for nb_root, nb_name, note_path, _, refs in note_items:
            display_note = f"{nb_name} / {note_path}" if multi_nb else note_path
            for ref in refs:
                rel_path = ref["rel_path"]
                local_path = self._resolve_local_file(nb_root, note_path, rel_path)
                dedupe_key = os.path.realpath(local_path) if local_path else f"{nb_root}::{note_path}::{rel_path}"
                if dedupe_key in seen:
                    entry = seen[dedupe_key]
                    if display_note not in entry["notes"]:
                        entry["notes"].append(display_note)
                    continue
                size = 0
                if local_path:
                    try:
                        size = os.path.getsize(local_path)
                    except OSError:
                        size = 0
                entry = {
                    "rel_path": rel_path,
                    "note_label": display_note,
                    "notes": [display_note],
                    "local_path": local_path or rel_path,
                    "size": size,
                }
                seen[dedupe_key] = entry
                ordered.append(entry)
        ordered.sort(key=lambda item: (-item["size"], item["rel_path"]))
        return ordered

    def _build_pending_entries(self, pending_images):
        if not pending_images:
            return []
        max_size = max(item["size"] for item in pending_images)
        entries = []
        for item in pending_images[:MAX_PENDING_DISPLAY]:
            note_sub = (
                item["note_label"]
                if len(item["notes"]) == 1
                else f"{item['note_label']} 等 {len(item['notes'])} 篇"
            )
            ratio = round(item["size"] / max_size, 4) if max_size > 0 else 0.04
            if ratio <= 0:
                ratio = 0.01
            entries.append(
                {
                    "title": item["rel_path"],
                    "sub_text": note_sub,
                    "time": self._format_bytes(item["size"]),
                    "ratio": ratio,
                    "tone": "assets",
                    "tooltip": item["local_path"],
                }
            )
        rest_items = pending_images[MAX_PENDING_DISPLAY:]
        if rest_items:
            rest_bytes = sum(item["size"] for item in rest_items)
            entries.append(
                {
                    "title": f"其余 {len(rest_items)} 张待上传图片",
                    "time": self._format_bytes(rest_bytes),
                }
            )
        return entries

    def _format_recent_logs(self, logs):
        entries = []
        for raw in logs[:MAX_RECENT_LOGS]:
            title = str(raw.get("title") or "").strip()
            time_str = str(raw.get("time") or "").strip()
            if not title or not time_str:
                continue
            status = str(raw.get("status") or "").strip()
            tone = str(raw.get("tone") or "").strip()
            if status in ("已上传", "通过", "已连通"):
                status = "成功"
            elif status == "未连通" or any(k in status for k in ("失败", "异常", "未找到", "不可达")):
                status = "失败"
            if not tone:
                if status == "成功":
                    tone = "success"
                elif status == "失败":
                    tone = "danger"
                else:
                    tone = "info"
            entry = {
                "title": title,
                "status": status or "成功",
                "time": time_str,
                "tone": tone,
            }
            if raw.get("tooltip"):
                entry["tooltip"] = str(raw["tooltip"])
            entries.append(entry)
        return entries

    def _is_configured(self, config):
        if config["provider"] in ("r2", "s3"):
            _, err = self._parse_s3_credentials(config)
        else:
            _, err = self._parse_github_credentials(config)
        return not err

    def _build_page(self, config, stats, logs, selected, note_items, status_override=None):
        provider_label = PROVIDER_LABELS.get(config["provider"], config["provider"])
        multi_nb = len(selected) > 1
        pending_images = self._collect_pending_images(note_items, multi_nb)
        pending_count = len(pending_images)
        pending_bytes = sum(item["size"] for item in pending_images)

        configured = self._is_configured(config)
        if status_override is not None:
            page_status = status_override
        elif not selected:
            page_status = "未选择笔记本"
        elif pending_count > 0:
            page_status = f"待上传 {pending_count} 张"
        else:
            page_status = ""

        uploaded_total = int(stats.get("uploaded_total", 0) or 0)
        queue_meta = (
            f"待上传 {pending_count} 张 · {self._format_bytes(pending_bytes)} · 累计 {uploaded_total} 张"
            if pending_count > 0
            else f"待上传 0 张 · 累计 {uploaded_total} 张"
        )
        pending_empty = (
            "请先在上方勾选要同步的笔记本"
            if not selected
            else "已选笔记本内所有图片均已外链化"
        )
        pending_entries = self._build_pending_entries(pending_images)
        recent_entries = self._format_recent_logs(logs)
        channel_badge = f"{provider_label} · 已配置" if configured else "未配置通道"

        if configured and pending_count > 0:
            header_actions = [
                {
                    "id": "upload_now",
                    "label": f"上传 {pending_count} 张图片",
                    "variant": "primary",
                }
            ]
        else:
            header_actions = [
                {
                    "id": "rescan",
                    "label": "重新扫描",
                    "variant": "secondary",
                }
            ]

        nb_options = []
        for entry in self.known_notebooks:
            path = entry["path"]
            opt = {
                "value": path,
                "label": entry["name"],
                "detail": self._display_path(path),
                "tooltip": path,
            }
            if entry.get("active"):
                opt["badge"] = "当前"
            nb_options.append(opt)
        nb_meta = f"已选 {len(selected)} / {len(nb_options)}" if nb_options else ""

        endpoint_placeholder = (
            "https://<account>.r2.cloudflarestorage.com"
            if config["provider"] == "r2"
            else "https://s3.amazonaws.com 或兼容端点"
        )
        public_domain_placeholder = (
            "https://pub-xxx.r2.dev 或自定义域名（R2 必填）"
            if config["provider"] == "r2"
            else "https://img.example.com（可留空）"
        )

        return {
            "title": "图床上传",
            "status": page_status,
            "header_actions": header_actions,
            "cards": [
                {
                    "id": "notebooks-card",
                    "title": "同步笔记本",
                    "meta": nb_meta,
                    "items": [
                        {
                            "kind": "checklist",
                            "id": "selected_notebooks",
                            "value": selected,
                            "options": nb_options,
                            "empty_text": "暂无已打开的笔记本",
                        }
                    ],
                },
                {
                    "id": "queue-card",
                    "title": "待上传队列",
                    "meta": queue_meta,
                    "items": [
                        {
                            "kind": "log_list",
                            "id": "pending_list",
                            "empty_text": pending_empty,
                            "entries": pending_entries,
                        },
                        {
                            "kind": "log_list",
                            "id": "recent_logs",
                            "label": "最近上传",
                            "empty_text": "暂无上传记录",
                            "entries": recent_entries,
                        },
                    ],
                },
                {
                    "id": "channel-card",
                    "title": "通道与上传规则",
                    "badge": channel_badge,
                    "collapsible": True,
                    "default_collapsed": configured,
                    "items": [
                        {
                            "kind": "segmented",
                            "id": "provider",
                            "label": "图床通道",
                            "value": config["provider"],
                            "options": [
                                {"value": "r2", "label": "Cloudflare R2"},
                                {"value": "s3", "label": "S3 兼容"},
                                {"value": "github", "label": "GitHub 仓库"},
                            ],
                        },
                        {
                            "kind": "group",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                            "fields": [
                                {
                                    "kind": "input",
                                    "id": "s3_endpoint",
                                    "label": "Endpoint 地址",
                                    "layout": "stacked",
                                    "value": config["s3_endpoint"],
                                    "placeholder": endpoint_placeholder,
                                },
                                {
                                    "kind": "input",
                                    "id": "s3_bucket",
                                    "label": "Bucket 存储桶",
                                    "layout": "stacked",
                                    "value": config["s3_bucket"],
                                    "placeholder": "存储桶名（支持 bucket/prefix）",
                                },
                            ],
                        },
                        {
                            "kind": "group",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                            "fields": [
                                {
                                    "kind": "input",
                                    "id": "s3_access_key",
                                    "label": "Access Key ID",
                                    "layout": "stacked",
                                    "value": config["s3_access_key"],
                                    "placeholder": "对象存储访问公钥 ID",
                                },
                                {
                                    "kind": "secret",
                                    "id": "s3_secret_key",
                                    "label": "Secret Access Key",
                                    "layout": "stacked",
                                    "value": config["s3_secret_key"],
                                    "placeholder": "对象存储访问密钥",
                                },
                            ],
                        },
                        {
                            "kind": "group",
                            "visible_when": {"control_id": "provider", "equals": "github"},
                            "fields": [
                                {
                                    "kind": "input",
                                    "id": "gh_repo",
                                    "label": "仓库 (owner/repo)",
                                    "layout": "stacked",
                                    "value": config["gh_repo"],
                                    "placeholder": "例如 username/image-assets",
                                },
                                {
                                    "kind": "input",
                                    "id": "gh_branch",
                                    "label": "目标分支",
                                    "layout": "stacked",
                                    "value": config["gh_branch"],
                                    "placeholder": "main",
                                },
                            ],
                        },
                        {
                            "kind": "group",
                            "visible_when": {"control_id": "provider", "equals": "github"},
                            "fields": [
                                {
                                    "kind": "secret",
                                    "id": "gh_token",
                                    "label": "Personal Access Token",
                                    "layout": "stacked",
                                    "value": config["gh_token"],
                                    "placeholder": "具备 repo 写入权限的 GitHub Token",
                                },
                                {
                                    "kind": "segmented",
                                    "id": "gh_cdn",
                                    "label": "外链加速",
                                    "layout": "stacked",
                                    "value": config["gh_cdn"],
                                    "options": [
                                        {"value": "jsdelivr", "label": "jsDelivr 加速"},
                                        {"value": "raw", "label": "GitHub 原生"},
                                    ],
                                },
                            ],
                        },
                        {
                            "kind": "group",
                            "fields": [
                                {
                                    "kind": "input",
                                    "id": "public_domain",
                                    "label": "公开访问域名",
                                    "layout": "stacked",
                                    "value": config["public_domain"],
                                    "placeholder": public_domain_placeholder,
                                    "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                                },
                                {
                                    "kind": "input",
                                    "id": "remote_prefix",
                                    "label": "远端目录前缀",
                                    "layout": "stacked",
                                    "value": config["remote_prefix"],
                                    "placeholder": "例如 notes（可留空）",
                                },
                            ],
                        },
                        {
                            "kind": "segmented",
                            "id": "naming_rule",
                            "label": "远端命名规则",
                            "value": config["naming_rule"],
                            "options": [
                                {"value": "ym_hash", "label": "年月哈希"},
                                {"value": "note_dir", "label": "按笔记目录"},
                                {"value": "original", "label": "原文件名"},
                            ],
                        },
                        {
                            "kind": "switch",
                            "id": "auto_upload",
                            "label": "保存笔记时自动上传本地图片（保留本地原图）",
                            "value": bool(config["auto_upload"]),
                        },
                        {
                            "kind": "actions",
                            "buttons": [
                                {"id": "test_conn", "label": "测试连接", "variant": "secondary"},
                                {"id": "save_config", "label": "保存配置", "variant": "primary"},
                            ],
                        },
                    ],
                },
            ],
        }

    # ---- JSON-RPC dispatch ----

    def _save_immediate_config(self, bridge, stored_cfg, config, incoming_values):
        if not incoming_values:
            return
        immediate_keys = ("selected_notebooks", "auto_upload", "naming_rule", "gh_cdn")
        changed_immediate = False
        for k in immediate_keys:
            if k in incoming_values and stored_cfg.get(k) != config.get(k):
                stored_cfg[k] = config[k]
                changed_immediate = True
        if changed_immediate:
            self._save_config(bridge, stored_cfg)

    def handle(self, bridge, method, params):
        if method == "plugin.initialize":
            self.notebook_root = self._norm_nb_path(params.get("notebook_root") or "")
            self.data_dir = str(params.get("data_dir") or "")
            self.locale = str(params.get("locale") or "zh")
            caps = set(params.get("ui_capabilities") or [])
            self.supports_checklist = "checklist" in caps
            self.supports_multi_notebook = "multi_notebook" in caps
            raw_nbs = params.get("notebooks")
            if isinstance(raw_nbs, list):
                self.known_notebooks = [
                    {
                        "path": self._norm_nb_path(p),
                        "name": os.path.basename(self._norm_nb_path(p)) or str(p),
                        "active": self._norm_nb_path(p) == self.notebook_root,
                    }
                    for p in raw_nbs
                    if str(p or "").strip()
                ]
            return {"ready": True}

        if method == "plugin.render":
            stored_cfg, stats, logs = self._load_state(bridge)
            incoming_values = params.get("values") if isinstance(params.get("values"), dict) else {}
            if incoming_values:
                merged = dict(stored_cfg)
                merged.update(incoming_values)
                config = self._normalize_config(merged)
                self._save_immediate_config(bridge, stored_cfg, config, incoming_values)
            else:
                config = stored_cfg
            selected = self._selected_notebooks(config)
            _, _, note_items = self._scan_notebooks(bridge, selected)
            page = self._build_page(config, stats, logs, selected, note_items)
            return {"page": page}

        if method == "plugin.action":
            action_id = str(params.get("action_id") or "")
            incoming_values = params.get("values") if isinstance(params.get("values"), dict) else {}
            prev_config, stats, logs = self._load_state(bridge)
            merged = dict(prev_config)
            merged.update(incoming_values)
            config = self._normalize_config(merged)
            selected = self._selected_notebooks(config)

            if action_id == "rescan":
                self._save_immediate_config(bridge, prev_config, config, incoming_values)
                scanned_notes, total_refs, note_items = self._scan_notebooks(bridge, selected)
                if not selected:
                    ok = False
                    tone = "warning"
                    status = "请先勾选至少一个笔记本"
                elif total_refs == 0:
                    ok = True
                    tone = "success"
                    status = f"已扫描 {scanned_notes} 篇笔记，未发现待上传图片"
                else:
                    ok = True
                    tone = "success"
                    pending_count = len(self._collect_pending_images(note_items, len(selected) > 1))
                    status = f"已扫描 {scanned_notes} 篇笔记，发现 {pending_count} 张待上传图片"
                page = self._build_page(config, stats, logs, selected, note_items, status_override=status)
                return {"ok": ok, "tone": tone, "values": config, "page": page, "status": status}

            if action_id == "save_config":
                _, _, note_items = self._scan_notebooks(bridge, selected)
                if config["provider"] in ("r2", "s3"):
                    _, cred_err = self._parse_s3_credentials(config)
                else:
                    _, cred_err = self._parse_github_credentials(config)
                if cred_err:
                    ok = False
                    tone = "danger"
                    status = cred_err
                else:
                    self._save_config(bridge, config)
                    ok = True
                    tone = "success"
                    status = "已保存图床配置"
                page = self._build_page(config, stats, logs, selected, note_items, status_override=status)
                return {"ok": ok, "tone": tone, "values": config, "page": page, "status": status}

            if action_id == "test_conn":
                _, _, note_items = self._scan_notebooks(bridge, selected)
                ok, msg = self._test_connection(config)
                tone = "success" if ok else "danger"
                logs = self._append_log(
                    logs,
                    f"连接测试（{PROVIDER_LABELS[config['provider']]}）",
                    "成功" if ok else "失败",
                    tone=tone,
                    tooltip=msg,
                )
                status = msg
                self._save_logs(bridge, logs)
                page = self._build_page(config, stats, logs, selected, note_items, status_override=status)
                return {"ok": ok, "tone": tone, "values": config, "page": page, "status": status}

            if action_id == "upload_now":
                stats, logs, status, uploaded_count, failed_count, cred_err = self._execute_upload(
                    bridge, config, stats, logs, selected
                )
                if not selected:
                    ok = False
                    tone = "warning"
                elif cred_err or (failed_count > 0 and uploaded_count == 0):
                    ok = False
                    tone = "danger"
                elif failed_count > 0:
                    ok = False
                    tone = "warning"
                elif uploaded_count == 0:
                    ok = True
                    tone = "info"
                    self._save_config(bridge, config)
                else:
                    ok = True
                    tone = "success"
                    self._save_config(bridge, config)
                _, _, note_items = self._scan_notebooks(bridge, selected)
                page = self._build_page(config, stats, logs, selected, note_items, status_override=status)
                return {"ok": ok, "tone": tone, "values": config, "page": page, "status": status}

            _, _, note_items = self._scan_notebooks(bridge, selected)
            status = f"未知操作：{action_id}"
            page = self._build_page(config, stats, logs, selected, note_items, status_override=status)
            return {"ok": False, "tone": "warning", "values": config, "page": page, "status": status}

        if method == "plugin.event":
            event_name = params.get("event")
            if event_name == "note.saved":
                config, stats, logs = self._load_state(bridge)
                if config.get("auto_upload") and self._is_configured(config):
                    note_path = str(params.get("path") or "").strip()
                    event_nb = self._norm_nb_path(params.get("notebook_root") or self.notebook_root)
                    selected = self._selected_notebooks(config)
                    if note_path and event_nb and event_nb in selected:
                        self._execute_upload(
                            bridge,
                            config,
                            stats,
                            logs,
                            selected,
                            target_note=(event_nb, note_path),
                        )
            return {"handled": True}

        if method == "plugin.shutdown":
            return {"ok": True}

        raise ValueError(f"unsupported method: {method}")


def main():
    plugin = ImageHostPlugin()
    bridge = HostBridge()
    bridge.serve(plugin.handle)


if __name__ == "__main__":
    main()
