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
import shutil
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
    "upload_scope": "active_note",
    "remote_prefix": "",
    "auto_upload": True,
    "naming_rule": "ym_hash",
    "after_upload": "replace_keep",
}

PROVIDER_LABELS = {
    "r2": "Cloudflare R2",
    "s3": "S3 兼容存储",
    "github": "GitHub 仓库图床",
}

NAMING_LABELS = {
    "ym_hash": "按年月哈希",
    "note_dir": "按笔记同名目录",
    "original": "保持原文件名",
}

VALID_PROVIDERS = {"r2", "s3", "github"}
VALID_GH_CDNS = {"jsdelivr", "raw"}
VALID_NAMING_RULES = {"ym_hash", "note_dir", "original"}
VALID_AFTER_UPLOAD = {"replace_keep", "replace_clean"}
MAX_LOGS = 20

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
        self.downloads_dir = os.path.expanduser("~/Downloads")
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
                clean_logs.append(item)
        return config, {"uploaded_total": uploaded_total}, clean_logs

    def _save_config(self, bridge, config):
        bridge.call_host("host.storage.set", {"key": "config", "value": config})

    def _save_stats(self, bridge, stats):
        bridge.call_host("host.storage.set", {"key": "stats", "value": stats})

    def _save_logs(self, bridge, logs):
        bridge.call_host("host.storage.set", {"key": "logs", "value": logs[:MAX_LOGS]})

    def _append_log(self, logs, title, status_text):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        entry = {"title": title, "time": now_str}
        if status_text:
            entry["status"] = status_text
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

        if not cfg["upload_scope"]:
            cfg["upload_scope"] = "active_note"

        gh_cdn = str(raw.get("gh_cdn", cfg["gh_cdn"])).strip()
        if gh_cdn in VALID_GH_CDNS:
            cfg["gh_cdn"] = gh_cdn

        if "auto_upload" in raw:
            cfg["auto_upload"] = bool(raw["auto_upload"])

        naming_rule = str(raw.get("naming_rule", cfg["naming_rule"])).strip()
        if naming_rule in VALID_NAMING_RULES:
            cfg["naming_rule"] = naming_rule

        after_upload = str(raw.get("after_upload", raw.get("post_process", cfg["after_upload"]))).strip()
        if after_upload in VALID_AFTER_UPLOAD:
            cfg["after_upload"] = after_upload

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

    def _archive_dir(self):
        return os.path.join(self.downloads_dir, "Block-Image-Archive")

    def _archive_local_files(self, bridge, archived_candidates):
        if not self.notebook_root or not archived_candidates:
            return
        _, _, remaining = self._scan_notes(bridge)
        still_referenced = set()
        for note_path, _, refs in remaining:
            for _, _, _, rel_path, _ in refs:
                resolved = self._resolve_local_file(note_path, rel_path)
                if resolved:
                    still_referenced.add(os.path.realpath(resolved))

        root_real = os.path.realpath(self.notebook_root)
        archive_root = self._archive_dir()
        for real_local_path, local_path in archived_candidates.items():
            if real_local_path in still_referenced or not os.path.isfile(local_path):
                continue
            try:
                rel_from_root = os.path.relpath(local_path, root_real)
                if rel_from_root.startswith(".."):
                    continue
                dest_path = os.path.join(archive_root, rel_from_root)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.move(local_path, dest_path)
            except Exception:
                continue

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
        archived_candidates = {}
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
                    logs = self._append_log(logs, rel_path, "未找到本地文件")
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
                        logs = self._append_log(logs, f"{note_path} -> {os.path.basename(local_path)}", "已上传")
                    except urllib.error.HTTPError as err:
                        failed_count += 1
                        logs = self._append_log(logs, os.path.basename(local_path), f"上传失败 HTTP {err.code}")
                        pieces.append(content[start:end])
                        cursor = end
                        continue
                    except urllib.error.URLError as err:
                        failed_count += 1
                        logs = self._append_log(logs, os.path.basename(local_path), f"网络不可达: {err.reason}")
                        pieces.append(content[start:end])
                        cursor = end
                        continue
                    except Exception as err:
                        failed_count += 1
                        logs = self._append_log(logs, os.path.basename(local_path), f"上传异常: {err}")
                        pieces.append(content[start:end])
                        cursor = end
                        continue

                pieces.append(f"![{alt}]({remote_url}{suffix})")
                note_changed = True
                archived_candidates[real_local] = local_path
                cursor = end

            pieces.append(content[cursor:])
            if note_changed:
                new_content = "".join(pieces)
                try:
                    bridge.call_host("host.notes.write", {"path": note_path, "content": new_content})
                    updated_notes += 1
                except Exception as err:
                    failed_count += 1
                    logs = self._append_log(logs, note_path, f"写回笔记失败: {err}")

        if config.get("after_upload") == "replace_clean" and archived_candidates:
            self._archive_local_files(bridge, archived_candidates)

        self._save_stats(bridge, stats)
        self._save_logs(bridge, logs)

        _, remaining_refs, _ = self._scan_notes(bridge)
        if failed_count > 0 and uploaded_count == 0:
            status = (
                f"已扫描 {scanned_notes} 篇笔记（{total_refs} 处本地图片），"
                f"本次 {failed_count} 项未能上传，请查看上传记录"
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

    def _build_scope_options(self, active_note, note_items, total_refs):
        active_label = f"当前打开笔记（{active_note}）" if active_note else "当前打开笔记（未打开）"
        all_label = (
            f"整个笔记本（{len(note_items)} 篇含图笔记，共 {total_refs} 张）"
            if total_refs > 0
            else "整个笔记本（全部笔记）"
        )
        options = [
            {"value": "active_note", "label": active_label},
            {"value": "all", "label": all_label},
        ]
        for note_path, _, refs in note_items:
            options.append({"value": f"note:{note_path}", "label": f"笔记：{note_path}（{len(refs)} 张）"})
        return options

    def _filter_scoped_items(self, scope, active_note, note_items):
        if scope == "active_note":
            if not active_note:
                return []
            return [item for item in note_items if item[0] == active_note]
        if scope.startswith("note:"):
            target = scope[len("note:") :]
            return [item for item in note_items if item[0] == target]
        return list(note_items)

    def _preview_remote_url(self, config, sample_note, sample_bytes, sample_rel):
        naming_rule = config.get("naming_rule", "ym_hash")
        if config["provider"] in ("r2", "s3"):
            bp = config.get("s3_bucket", "").strip().strip("/")
            bp_parts = bp.split("/", 1) if bp else []
            bucket = bp_parts[0].strip() if bp_parts else ""
            bucket_prefix = bp_parts[1].strip("/") if len(bp_parts) > 1 else ""
            prefix = self._effective_prefix(config, bucket_prefix)
            remote_key = self._build_remote_key(naming_rule, prefix, sample_note, sample_bytes, sample_rel)
            encoded_key = "/".join(urllib.parse.quote(seg, safe="") for seg in remote_key.split("/"))
            public_domain = config.get("public_domain", "").strip().rstrip("/")
            if public_domain:
                if not public_domain.startswith(("http://", "https://")):
                    public_domain = "https://" + public_domain
                return f"{public_domain}/{encoded_key}"
            endpoint = config.get("s3_endpoint", "").strip().rstrip("/")
            if endpoint and bucket:
                if not endpoint.startswith(("http://", "https://")):
                    endpoint = "https://" + endpoint
                return f"{endpoint}/{urllib.parse.quote(bucket, safe='')}/{encoded_key}"
            return f"https://img.example.com/{encoded_key}"

        prefix = self._effective_prefix(config)
        branch = config["gh_branch"] or "main"
        remote_key = self._build_remote_key(naming_rule, prefix, sample_note, sample_bytes, sample_rel)
        encoded_path = "/".join(urllib.parse.quote(seg, safe="") for seg in remote_key.split("/"))
        owner, repo, _ = self._parse_github_repo(config.get("gh_repo", ""))
        if not owner or not repo:
            owner, repo = "owner", "repo"
        branch_quoted = urllib.parse.quote(branch, safe="")
        if config.get("gh_cdn", "jsdelivr") == "jsdelivr":
            return f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch_quoted}/{encoded_path}"
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch_quoted}/{encoded_path}"

    def _build_preview_item(self, config, active_note, scoped_items):
        provider_label = PROVIDER_LABELS.get(config["provider"], config["provider"])
        naming_label = NAMING_LABELS.get(config["naming_rule"], config["naming_rule"])
        scope_notes_count = len(scoped_items)
        scope_refs_count = sum(len(refs) for _, _, refs in scoped_items)

        if scope_refs_count > 0:
            sample_note, _, first_refs = scoped_items[0]
            _, _, sample_alt, sample_rel, sample_suffix = first_refs[0]
            local_path = self._resolve_local_file(sample_note, sample_rel)
            sample_bytes = b"block-image-host-preview"
            if local_path:
                try:
                    with open(local_path, "rb") as fh:
                        sample_bytes = fh.read()
                except Exception:
                    pass
        else:
            sample_note = active_note or "notes/demo.md"
            sample_alt = "示例图片"
            sample_rel = "assets/cover.png"
            sample_suffix = ""
            sample_bytes = b"block-image-host-preview"

        sample_url = self._preview_remote_url(config, sample_note, sample_bytes, sample_rel)
        before_text = f"![{sample_alt}]({sample_rel}{sample_suffix})"
        after_text = f"![{sample_alt}]({sample_url}{sample_suffix})"

        lines = []
        for note_path, _, refs in scoped_items:
            for _, _, _, rel_path, _ in refs:
                lines.append(f"{note_path} -> {rel_path}")
                if len(lines) >= 8:
                    break
            if len(lines) >= 8:
                break
        if scope_refs_count > 8:
            lines.append(f"... 另有 {scope_refs_count - 8} 处")
        if not lines:
            lines.append("当前范围内暂无待上传本地图片（上方以示例 assets/cover.png 演示外链格式）")

        return {
            "kind": "preview",
            "preview": {
                "variant": "diff",
                "title": "外链与替换实时预览",
                "badge": f"{provider_label} · {naming_label}",
                "subtitle": f"待处理 {scope_notes_count} 篇笔记，共 {scope_refs_count} 张本地图片",
                "before": before_text,
                "after": after_text,
                "body": "\n".join(lines),
            },
        }

    def _build_page(self, config, stats, logs, note_items, total_refs, active_note, status_override=None):
        provider_label = PROVIDER_LABELS.get(config["provider"], config["provider"])
        scope_options = self._build_scope_options(active_note, note_items, total_refs)
        valid_scope_values = {opt["value"] for opt in scope_options}
        effective_scope = config["upload_scope"] if config["upload_scope"] in valid_scope_values else "active_note"
        scoped_items = self._filter_scoped_items(effective_scope, active_note, note_items)
        scoped_pending = sum(len(refs) for _, _, refs in scoped_items)

        page_status = status_override or f"{provider_label} · 待上传 {scoped_pending} 张（全库 {total_refs} 张）"
        uploaded_total = int(stats.get("uploaded_total", 0) or 0)
        preview_item = self._build_preview_item(config, active_note, scoped_items)

        return {
            "title": "图床上传",
            "status": page_status,
            "header_actions": [
                {
                    "id": "upload_pending",
                    "label": "扫描并上传图片",
                    "variant": "primary",
                }
            ],
            "cards": [
                {
                    "id": "channel-card",
                    "title": "图床通道配置",
                    "items": [
                        {
                            "kind": "segmented",
                            "id": "provider",
                            "label": "图床通道",
                            "value": config["provider"],
                            "options": [
                                {"value": "r2", "label": "Cloudflare R2"},
                                {"value": "s3", "label": "S3 兼容存储"},
                                {"value": "github", "label": "GitHub 仓库图床"},
                            ],
                        },
                        {
                            "kind": "input",
                            "id": "s3_endpoint",
                            "label": "Endpoint 地址",
                            "value": config["s3_endpoint"],
                            "placeholder": "例如 https://<account>.r2.cloudflarestorage.com 或 S3 Endpoint",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                        },
                        {
                            "kind": "input",
                            "id": "s3_bucket",
                            "label": "Bucket 存储桶名",
                            "value": config["s3_bucket"],
                            "placeholder": "填写存储桶名称（支持 bucket/prefix 前缀）",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                        },
                        {
                            "kind": "input",
                            "id": "s3_access_key",
                            "label": "Access Key ID",
                            "value": config["s3_access_key"],
                            "placeholder": "对象存储访问公钥 ID",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                        },
                        {
                            "kind": "secret",
                            "id": "s3_secret_key",
                            "label": "Secret Access Key",
                            "value": config["s3_secret_key"],
                            "placeholder": "对象存储访问密钥",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                        },
                        {
                            "kind": "input",
                            "id": "public_domain",
                            "label": "公开访问域名",
                            "value": config["public_domain"],
                            "placeholder": "例如 https://img.example.com（可留空使用默认地址）",
                            "visible_when": {"control_id": "provider", "in": ["r2", "s3"]},
                        },
                        {
                            "kind": "input",
                            "id": "gh_repo",
                            "label": "仓库 (owner/repo)",
                            "value": config["gh_repo"],
                            "placeholder": "例如 username/image-assets",
                            "visible_when": {"control_id": "provider", "equals": "github"},
                        },
                        {
                            "kind": "input",
                            "id": "gh_branch",
                            "label": "目标分支",
                            "value": config["gh_branch"],
                            "placeholder": "main",
                            "visible_when": {"control_id": "provider", "equals": "github"},
                        },
                        {
                            "kind": "secret",
                            "id": "gh_token",
                            "label": "Personal Access Token",
                            "value": config["gh_token"],
                            "placeholder": "具备 repo 写入权限的 GitHub Token",
                            "visible_when": {"control_id": "provider", "equals": "github"},
                        },
                        {
                            "kind": "segmented",
                            "id": "gh_cdn",
                            "label": "外链加速方式",
                            "value": config["gh_cdn"],
                            "options": [
                                {"value": "jsdelivr", "label": "jsDelivr 加速"},
                                {"value": "raw", "label": "GitHub 原生"},
                            ],
                            "visible_when": {"control_id": "provider", "equals": "github"},
                        },
                        {
                            "kind": "actions",
                            "buttons": [
                                {"id": "save_config", "label": "保存配置", "variant": "primary"},
                                {"id": "test_conn", "label": "测试连接", "variant": "secondary"},
                            ],
                        },
                    ],
                },
                {
                    "id": "policy-card",
                    "title": "上传范围与路径策略",
                    "items": [
                        {
                            "kind": "select",
                            "id": "upload_scope",
                            "label": "上传范围",
                            "value": effective_scope,
                            "options": scope_options,
                        },
                        {
                            "kind": "input",
                            "id": "remote_prefix",
                            "label": "远端目录前缀",
                            "value": config["remote_prefix"],
                            "placeholder": "可留空",
                        },
                        {
                            "kind": "switch",
                            "id": "auto_upload",
                            "label": "保存笔记时自动上传本地图片",
                            "value": bool(config["auto_upload"]),
                        },
                        {
                            "kind": "segmented",
                            "id": "naming_rule",
                            "label": "远端路径命名",
                            "value": config["naming_rule"],
                            "options": [
                                {"value": "ym_hash", "label": "按年月哈希"},
                                {"value": "note_dir", "label": "按笔记同名目录"},
                                {"value": "original", "label": "保持原文件名"},
                            ],
                        },
                        {
                            "kind": "segmented",
                            "id": "after_upload",
                            "label": "上传后处理",
                            "value": config["after_upload"],
                            "options": [
                                {"value": "replace_keep", "label": "保留本地原图"},
                                {"value": "replace_clean", "label": "移至下载目录归档"},
                            ],
                        },
                        {
                            "kind": "actions",
                            "visible_when": {"control_id": "after_upload", "equals": "replace_clean"},
                            "buttons": [
                                {"id": "reveal_archive", "label": "打开归档目录", "variant": "secondary"},
                            ],
                        },
                    ],
                },
                {
                    "id": "preview-card",
                    "title": "实时外链与替换预览",
                    "items": [
                        preview_item,
                    ],
                },
                {
                    "id": "status-card",
                    "title": "图床概览与记录",
                    "items": [
                        {
                            "kind": "metrics",
                            "metrics": [
                                {"label": "当前通道", "value": provider_label},
                                {"label": "范围内待上传", "value": f"{scoped_pending} 张"},
                                {"label": "累计已上传", "value": f"{uploaded_total} 张"},
                            ],
                        },
                        {
                            "kind": "log_list",
                            "empty_text": "暂无上传记录",
                            "entries": logs,
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
            downloads_dir = str(params.get("downloads_dir") or "").strip()
            self.downloads_dir = os.path.expanduser(downloads_dir) if downloads_dir else os.path.expanduser("~/Downloads")
            self.locale = str(params.get("locale") or "zh")
            return {"ready": True}

        if method == "plugin.render":
            config, stats, logs = self._load_state(bridge)
            incoming_values = params.get("values") if isinstance(params.get("values"), dict) else {}
            if incoming_values:
                merged = dict(config)
                merged.update(incoming_values)
                config = self._normalize_config(merged)
            active_note = self._get_active_note(bridge)
            _, total_refs, note_items = self._scan_notes(bridge)
            page = self._build_page(config, stats, logs, note_items, total_refs, active_note)
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
                _, total_refs, note_items = self._scan_notes(bridge)
                provider_label = PROVIDER_LABELS.get(config["provider"], config["provider"])
                status = f"配置已保存 · 当前通道 {provider_label}，全库待上传 {total_refs} 张"
                page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            if action_id == "test_conn":
                scanned_notes, total_refs, note_items = self._scan_notes(bridge)
                ok, msg = self._test_connection(config)
                if ok:
                    logs = self._append_log(logs, f"连接测试 ({PROVIDER_LABELS[config['provider']]})", "成功")
                    status = f"{msg} · 已扫描 {scanned_notes} 篇笔记（待上传 {total_refs} 张）"
                else:
                    logs = self._append_log(logs, f"连接测试 ({PROVIDER_LABELS[config['provider']]})", "未就绪")
                    status = f"{msg}（已扫描 {scanned_notes} 篇笔记，待上传 {total_refs} 张）"
                self._save_logs(bridge, logs)
                page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            if action_id == "reveal_archive":
                archive_dir = self._archive_dir()
                os.makedirs(archive_dir, exist_ok=True)
                _, total_refs, note_items = self._scan_notes(bridge)
                status = f"已定位归档目录：{archive_dir}"
                page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
                return {"values": config, "page": page, "status": status, "reveal_path": archive_dir}

            if action_id == "upload_pending":
                scope = config.get("upload_scope", "active_note")
                target_note = None
                if scope == "active_note":
                    if not active_note:
                        _, total_refs, note_items = self._scan_notes(bridge)
                        status = "当前未打开笔记，请先打开笔记或将上传范围切换为「整个笔记本」"
                        page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
                        return {"values": config, "page": page, "status": status}
                    target_note = active_note
                elif scope.startswith("note:"):
                    target_note = scope[len("note:") :]

                stats, logs, status, _, _ = self._execute_upload(
                    bridge, config, stats, logs, target_note_path=target_note
                )
                _, total_refs, note_items = self._scan_notes(bridge)
                page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
                return {"values": config, "page": page, "status": status}

            _, total_refs, note_items = self._scan_notes(bridge)
            status = f"未知操作：{action_id}"
            page = self._build_page(config, stats, logs, note_items, total_refs, active_note, status_override=status)
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
