#!/usr/bin/env python3
"""Official Block plugin: block-image-host (Cloudflare R2 / S3 / GitHub image hosting)."""

import base64
import datetime
import hashlib
import hmac
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
    "upload_scope": "all",
    "remote_prefix": "",
    "auto_upload": True,
    "naming_rule": "ym_hash",
}

PROVIDER_LABELS = {
    "r2": "Cloudflare R2",
    "s3": "S3 兼容",
    "github": "GitHub 仓库",
}

VALID_PROVIDERS = {"r2", "s3", "github"}
VALID_GH_CDNS = {"jsdelivr", "raw"}
VALID_NAMING_RULES = {"ym_hash", "note_dir", "original"}
VALID_SCOPES = {"all", "active_note"}
MAX_LOGS = 20
MAX_PENDING_DISPLAY = 6
MAX_RECENT_LOGS = 5

IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")


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
        self.data_dir = ""
        self.locale = "zh"

    # ---- Storage helpers ----

    def _load_state(self, bridge):
        try:
            res = bridge.call_host("host.storage.get", {})
            data = res.get("data") or {}
        except Exception:
            data = {}
        raw_cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
        config = self._normalize_config(raw_cfg)
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
            "upload_scope",
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

        if cfg["upload_scope"] not in VALID_SCOPES:
            cfg["upload_scope"] = "all"

        gh_cdn = str(raw.get("gh_cdn", cfg["gh_cdn"])).strip()
        if gh_cdn in VALID_GH_CDNS:
            cfg["gh_cdn"] = gh_cdn

        if "auto_upload" in raw:
            cfg["auto_upload"] = bool(raw["auto_upload"])

        naming_rule = str(raw.get("naming_rule", cfg["naming_rule"])).strip()
        if naming_rule in VALID_NAMING_RULES:
            cfg["naming_rule"] = naming_rule

        return cfg

    def _effective_prefix(self, config, bucket_prefix=""):
        prefix = config["remote_prefix"].strip("/")
        return "/".join(part for part in (bucket_prefix.strip("/"), prefix) if part)

    # ---- Markdown & local asset scanning ----

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
        if lower.startswith(("http://", "https://", "data:", "file:", "mailto:", "//")):
            return None
        clean = stripped.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean or clean.startswith("/"):
            return None
        if len(clean) >= 2 and clean[1] == ":" and clean[0].isalpha():
            return None
        return clean

    def _extract_local_refs(self, content):
        """Return list of (start, end, alt, rel_asset_path, suffix) outside fenced code blocks."""
        refs = []
        in_fence = False
        offset = 0
        for line in content.splitlines(keepends=True):
            if FENCE_PATTERN.match(line):
                in_fence = not in_fence
                offset += len(line)
                continue
            if not in_fence:
                for match in IMAGE_PATTERN.finditer(line):
                    alt = match.group(1)
                    target, suffix = self._parse_image_inner(match.group(2))
                    rel_path = self._normalize_local_asset_ref(target)
                    if rel_path:
                        refs.append(
                            (
                                offset + match.start(),
                                offset + match.end(),
                                alt,
                                rel_path,
                                suffix,
                            )
                        )
            offset += len(line)
        return refs

    def _resolve_local_file(self, note_path, rel_asset_path):
        if not self.notebook_root:
            return None
        root_real = os.path.realpath(self.notebook_root)
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

    def _get_active_note(self, bridge):
        try:
            res = bridge.call_host("host.notes.active", {})
            return str(res.get("path") or "").strip()
        except Exception:
            return ""

    def _scan_notes(self, bridge, target_note_path=None):
        """Scan notes and return (note_count, total_refs, items_by_note)."""
        try:
            list_res = bridge.call_host("host.notes.list", {})
            notes = list_res.get("notes") or []
        except Exception:
            return 0, 0, []

        results = []
        total_refs = 0
        scanned_notes = 0
        for item in notes:
            note_path = item.get("path") if isinstance(item, dict) else None
            if not note_path:
                continue
            if target_note_path and note_path != target_note_path:
                continue
            scanned_notes += 1
            try:
                read_res = bridge.call_host("host.notes.read", {"path": note_path})
                content = read_res.get("content", "")
            except Exception:
                continue
            raw_refs = self._extract_local_refs(content)
            if self.notebook_root:
                refs = [
                    ref for ref in raw_refs
                    if self._resolve_local_file(note_path, ref[3]) is not None
                ]
            else:
                refs = raw_refs
            if refs:
                total_refs += len(refs)
                results.append((note_path, content, refs))
        return scanned_notes, total_refs, results

    # ---- Config parsing & validation ----

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
        prefix = self._effective_prefix(config, bucket_prefix)

        access_key = config.get("s3_access_key", "").strip()
        secret_key = config.get("s3_secret_key", "").strip()
        if not access_key or not secret_key:
            return None, "请完整填写 Access Key ID 与 Secret Access Key"

        region = "auto" if config["provider"] == "r2" else "us-east-1"
        if config["provider"] == "s3":
            host_match = re.search(r"s3[.-]([a-z0-9-]+)\.amazonaws\.com", parsed.netloc)
            if host_match:
                region = host_match.group(1)

        public_domain = config.get("public_domain", "").strip().rstrip("/")
        if public_domain and not public_domain.startswith(("http://", "https://")):
            public_domain = "https://" + public_domain

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

    def _s3_signed_request(self, creds, method, path_segments, query_params=None, body=b"", content_type=None):
        parsed = creds["parsed_endpoint"]
        base_path = parsed.path.rstrip("/")
        quoted_segs = [urllib.parse.quote(seg, safe="") for seg in path_segments if seg != ""]
        canonical_uri = f"{base_path}/" + "/".join(quoted_segs)
        if not canonical_uri.startswith("/"):
            canonical_uri = "/" + canonical_uri

        query_items = sorted((k, v) for k, v in (query_params or {}).items())
        canonical_qs = "&".join(
            f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(str(v), safe='~')}"
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()

    def _github_api_request(self, creds, method, api_url, payload=None):
        headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "block-image-host/1.1",
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})

    def _build_remote_key(self, naming_rule, prefix, note_path, file_bytes, rel_asset_path):
        filename = os.path.basename(urllib.parse.unquote(rel_asset_path)) or "image.png"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)

        if naming_rule == "note_dir":
            stem = note_path.replace("\\", "/")
            if stem.endswith(".md"):
                stem = stem[:-3]
            safe_parts = [
                re.sub(r"[^A-Za-z0-9._-]", "_", part)
                for part in stem.split("/")
                if part and part not in (".", "..")
            ]
            folder = "/".join(safe_parts) or "notes"
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

    def _upload_file(self, config, creds, note_path, local_path, rel_asset_path):
        with open(local_path, "rb") as fh:
            file_bytes = fh.read()
        remote_key = self._build_remote_key(
            config.get("naming_rule", "ym_hash"),
            creds.get("prefix", ""),
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
            encoded_key = "/".join(urllib.parse.quote(seg, safe="") for seg in remote_key.split("/"))
            if creds["public_domain"]:
                return f"{creds['public_domain']}/{encoded_key}"
            return f"{creds['endpoint']}/{urllib.parse.quote(creds['bucket'], safe='')}/{encoded_key}"

        encoded_path = "/".join(urllib.parse.quote(seg, safe="") for seg in remote_key.split("/"))
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
                return False, f"图床服务端返回 HTTP {err.code}，请核对凭证、区域或存储桶权限"
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
            return True, f"GitHub 仓库 {creds['owner']}/{creds['repo']}（分支 {creds['branch']}）连接成功"
        except urllib.error.HTTPError as err:
            return False, f"GitHub API 返回 HTTP {err.code}，请核对仓库名称或令牌权限"
        except urllib.error.URLError as err:
            return False, f"无法连接 GitHub API（{err.reason}）"
        except Exception as err:
            return False, f"连接校验失败：{err}"

    def _execute_upload(self, bridge, config, stats, logs, target_note_path=None):
        scanned_notes, total_refs, note_items = self._scan_notes(bridge, target_note_path=target_note_path)
        if config["provider"] in ("r2", "s3"):
            creds, err_msg = self._parse_s3_credentials(config)
        else:
            creds, err_msg = self._parse_github_credentials(config)

        if err_msg:
            status = f"已扫描 {scanned_notes} 篇笔记，发现 {total_refs} 处待上传本地图片；{err_msg}"
            return stats, logs, status, 0, total_refs

        if total_refs == 0:
            status = f"已扫描 {scanned_notes} 篇笔记，未发现待上传的本地图片"
            return stats, logs, status, 0, 0

        uploaded_cache = {}
        uploaded_count = 0
        failed_count = 0
        updated_notes = 0
        naming_rule = config.get("naming_rule", "ym_hash")

        for note_path, content, refs in note_items:
            pieces = []
            cursor = 0
            note_changed = False
            for start, end, alt, rel_path, suffix in refs:
                pieces.append(content[cursor:start])
                local_path = self._resolve_local_file(note_path, rel_path)
                if not local_path:
                    failed_count += 1
                    logs = self._append_log(logs, rel_path, "失败", tone="danger", tooltip="未找到本地文件")
                    pieces.append(content[start:end])
                    cursor = end
                    continue

                real_local = os.path.realpath(local_path)
                cache_key = (real_local, note_path if naming_rule == "note_dir" else "")
                if cache_key in uploaded_cache:
                    remote_url = uploaded_cache[cache_key]
                else:
                    try:
                        remote_url = self._upload_file(config, creds, note_path, local_path, rel_path)
                        uploaded_cache[cache_key] = remote_url
                        uploaded_count += 1
                        stats["uploaded_total"] = int(stats.get("uploaded_total", 0)) + 1
                        logs = self._append_log(
                            logs,
                            f"{note_path} -> {os.path.basename(local_path)}",
                            "成功",
                            tone="success",
                            tooltip=remote_url,
                        )
                    except urllib.error.HTTPError as err:
                        failed_count += 1
                        logs = self._append_log(
                            logs,
                            os.path.basename(local_path),
                            "失败",
                            tone="danger",
                            tooltip=f"HTTP {err.code}",
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

                pieces.append(f"![{alt}]({remote_url}{suffix})")
                note_changed = True
                cursor = end

            pieces.append(content[cursor:])
            if note_changed:
                new_content = "".join(pieces)
                try:
                    bridge.call_host("host.notes.write", {"path": note_path, "content": new_content})
                    updated_notes += 1
                except Exception as err:
                    failed_count += 1
                    logs = self._append_log(
                        logs,
                        note_path,
                        "失败",
                        tone="danger",
                        tooltip=f"写回笔记失败: {err}",
                    )

        self._save_stats(bridge, stats)
        self._save_logs(bridge, logs)

        _, remaining_refs, _ = self._scan_notes(bridge)
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
        return stats, logs, status, uploaded_count, remaining_refs

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

    def _filter_scoped_items(self, scope, active_note, note_items):
        if scope == "active_note":
            if not active_note:
                return []
            return [item for item in note_items if item[0] == active_note]
        return list(note_items)

    def _collect_pending_images(self, scoped_items):
        seen = {}
        ordered = []
        for note_path, _, refs in scoped_items:
            for _, _, _, rel_path, _ in refs:
                local_path = self._resolve_local_file(note_path, rel_path)
                dedupe_key = os.path.realpath(local_path) if local_path else f"{note_path}::{rel_path}"
                if dedupe_key in seen:
                    entry = seen[dedupe_key]
                    if note_path not in entry["notes"]:
                        entry["notes"].append(note_path)
                    continue
                size = 0
                if local_path:
                    try:
                        size = os.path.getsize(local_path)
                    except OSError:
                        size = 0
                entry = {
                    "rel_path": rel_path,
                    "note_path": note_path,
                    "notes": [note_path],
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
                item["note_path"]
                if len(item["notes"]) == 1
                else f"{item['note_path']} 等 {len(item['notes'])} 篇"
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

    def _build_page(self, config, stats, logs, note_items, active_note, status_override=None):
        provider_label = PROVIDER_LABELS.get(config["provider"], config["provider"])
        effective_scope = config["upload_scope"] if config["upload_scope"] in VALID_SCOPES else "all"
        scoped_items = self._filter_scoped_items(effective_scope, active_note, note_items)
        pending_images = self._collect_pending_images(scoped_items)
        pending_count = len(pending_images)
        pending_bytes = sum(item["size"] for item in pending_images)

        configured = self._is_configured(config)
        if status_override is not None:
            page_status = status_override
        elif configured and pending_count > 0:
            page_status = f"待上传 {pending_count} 张"
        else:
            page_status = ""

        uploaded_total = int(stats.get("uploaded_total", 0) or 0)
        scope_label = (
            f"待上传 {pending_count} 张 · {self._format_bytes(pending_bytes)}"
            if pending_count > 0
            else "待上传 0 张"
        )
        pending_entries = self._build_pending_entries(pending_images)
        recent_entries = self._format_recent_logs(logs)
        upload_btn_label = f"上传 {pending_count} 张图片" if pending_count > 0 else "重新扫描"
        channel_badge = f"{provider_label} · 已配置" if configured else "未配置通道"

        return {
            "title": "图床上传",
            "status": page_status,
            "header_actions": [
                {
                    "id": "upload_now",
                    "label": upload_btn_label,
                    "variant": "primary",
                }
            ] if configured else [],
            "cards": [
                {
                    "id": "queue-card",
                    "title": "待上传队列",
                    "meta": f"累计已上传 {uploaded_total} 张",
                    "items": [
                        {
                            "kind": "segmented",
                            "id": "upload_scope",
                            "label": scope_label,
                            "value": effective_scope,
                            "options": [
                                {"value": "all", "label": "整个笔记本"},
                                {"value": "active_note", "label": "当前笔记"},
                            ],
                        },
                        {
                            "kind": "log_list",
                            "id": "pending_list",
                            "empty_text": "当前范围内所有图片均已外链化",
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
                                    "placeholder": "https://<account>.r2.cloudflarestorage.com",
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
                                    "placeholder": "https://img.example.com（可留空）",
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

    def handle(self, bridge, method, params):
        if method == "plugin.initialize":
            self.notebook_root = str(params.get("notebook_root") or "")
            self.data_dir = str(params.get("data_dir") or "")
            self.locale = str(params.get("locale") or "zh")
            return {"ready": True}

        if method == "plugin.render":
            stored_cfg, stats, logs = self._load_state(bridge)
            incoming_values = params.get("values") if isinstance(params.get("values"), dict) else {}
            if incoming_values:
                merged = dict(stored_cfg)
                merged.update(incoming_values)
                config = self._normalize_config(merged)
                immediate_keys = ("upload_scope", "auto_upload", "naming_rule", "gh_cdn")
                changed_immediate = False
                for k in immediate_keys:
                    if stored_cfg.get(k) != config.get(k):
                        stored_cfg[k] = config[k]
                        changed_immediate = True
                if changed_immediate:
                    self._save_config(bridge, stored_cfg)
            else:
                config = stored_cfg
            active_note = self._get_active_note(bridge)
            _, _, note_items = self._scan_notes(bridge)
            page = self._build_page(config, stats, logs, note_items, active_note)
            return {"page": page}

        if method == "plugin.action":
            action_id = str(params.get("action_id") or "")
            incoming_values = params.get("values") if isinstance(params.get("values"), dict) else {}
            prev_config, stats, logs = self._load_state(bridge)
            merged = dict(prev_config)
            merged.update(incoming_values)
            config = self._normalize_config(merged)
            self._save_config(bridge, config)
            active_note = self._get_active_note(bridge)

            if action_id == "save_config":
                _, _, note_items = self._scan_notes(bridge)
                if config["provider"] in ("r2", "s3"):
                    _, cred_err = self._parse_s3_credentials(config)
                else:
                    _, cred_err = self._parse_github_credentials(config)
                status = cred_err or "已保存配置"
                page = self._build_page(config, stats, logs, note_items, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            if action_id == "test_conn":
                _, _, note_items = self._scan_notes(bridge)
                ok, msg = self._test_connection(config)
                logs = self._append_log(
                    logs,
                    f"连接测试（{PROVIDER_LABELS[config['provider']]}）",
                    "成功" if ok else "失败",
                    tone="success" if ok else "danger",
                    tooltip=msg,
                )
                status = msg
                self._save_logs(bridge, logs)
                page = self._build_page(config, stats, logs, note_items, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            if action_id == "upload_now":
                scope = config.get("upload_scope", "all")
                target_note = None
                if scope == "active_note":
                    if not active_note:
                        _, _, note_items = self._scan_notes(bridge)
                        status = "当前未打开笔记，请先打开笔记或将上传范围切换为「整个笔记本」"
                        page = self._build_page(config, stats, logs, note_items, active_note, status_override=status)
                        return {"values": config, "page": page, "status": status}
                    target_note = active_note

                stats, logs, status, _, _ = self._execute_upload(
                    bridge, config, stats, logs, target_note_path=target_note
                )
                _, _, note_items = self._scan_notes(bridge)
                page = self._build_page(config, stats, logs, note_items, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            _, _, note_items = self._scan_notes(bridge)
            status = f"未知操作：{action_id}"
            page = self._build_page(config, stats, logs, note_items, active_note, status_override=status)
            return {"values": config, "page": page, "status": status}

        if method == "plugin.event":
            event_name = params.get("event")
            if event_name == "note.saved":
                config, stats, logs = self._load_state(bridge)
                if config.get("auto_upload"):
                    note_path = params.get("path")
                    if note_path:
                        self._execute_upload(bridge, config, stats, logs, target_note_path=str(note_path))
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
