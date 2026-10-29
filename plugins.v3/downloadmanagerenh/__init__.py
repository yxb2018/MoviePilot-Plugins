"""
下载管理增强（DownloadManagerEnh）· V3 插件 · 第一版

目标：在下载管理下新增入口，补全 qBittorrent 各状态种子的海报与识别信息。
原生「下载管理」(GET /api/v1/download/) 只返回正在下载的种子，做种中/已暂停/已完成
的种子根本没有统一展示入口。本插件直接拉取 qB 全量种子，按 hash 匹配 MP 整理记录
（transfer_history）补全海报/片名/来源站，并标记 MP 管理的种子（qB 标签 MOVIEPILOT）。

合规红线（moviepilot-plugin-dev）：
- 只读宿主数据：下载器走 DownloaderHelper；整理记录走 TransferHistoryOper.list_by_hash。
- 不 import app.db.models；不改写任何 qB 状态（本版纯展示，无需卸载回滚）。
"""
from typing import Any, Dict, List, Optional, Tuple

try:  # 不同 MP 版本导入路径兼容
    from app.sdk.plugin import _PluginBase
except ImportError:  # pragma: no cover
    from app.plugins import _PluginBase

from app.sdk.services import DownloaderHelper
from app.sdk.logging import logger

LOG_TAG = "[下载管理增强] "


class DownloadManagerEnh(_PluginBase):
    # ------------------------------------------------------------------ 元数据
    plugin_name = "下载管理增强"
    plugin_desc = "补全 qBittorrent 各状态种子的海报与识别信息（原生仅展示下载中），并标记 MP 管理的种子。"
    plugin_icon = "downloadmanagerenh.png"
    plugin_version = "1.0.0"
    plugin_config_prefix = "downloadmanagerenh_"

    # ------------------------------------------------------------------ 配置项
    _enabled: bool = False
    _hide_downloading: bool = False
    _only_mp_tag: bool = False

    # qB 正在下载类状态：用于「隐藏下载中」时聚焦原生已覆盖的缺口
    _DOWNLOADING_STATES = {
        "downloading", "stalledDL", "queuedDL", "forcedDL",
        "checkingDL", "metaDL", "allocating", "moving",
    }
    # MP 用来标记其 qB 种子的标签
    _MP_TAG = "MOVIEPILOT"

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._hide_downloading = bool(config.get("hide_downloading", False))
            self._only_mp_tag = bool(config.get("only_mp_tag", False))

    def get_state(self) -> bool:
        return bool(self._enabled)

    # ------------------------------------------------------------------ 抽象方法补齐（_PluginBase 契约）
    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self) -> None:
        """纯展示插件，无后台调度，无需清理。"""
        return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, self._switch("enabled", "启用插件",
                                "开启后新增下载管理入口，补全各状态种子海报与识别信息")),
                            self._col(12, 4, self._switch("hide_downloading", "隐藏下载中的种子",
                                "原生已展示下载中，开启后聚焦做种/暂停/已完成等缺口")),
                            self._col(12, 4, self._switch("only_mp_tag", "仅显示 MP 管理的种子",
                                "只展示带 MOVIEPILOT 标签、由 MP 接管的种子")),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "hide_downloading": False,
            "only_mp_tag": False,
        }

    @staticmethod
    def _col(cols: int, md: int, content: dict) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [content],
        }

    @staticmethod
    def _switch(model: str, label: str, hint: str) -> dict:
        return {
            "component": "VSwitch",
            "props": {"model": model, "label": label, "hint": hint, "persistent-hint": True},
        }

    # ------------------------------------------------------------------ 入口页面
    def get_page(self) -> Optional[List[dict]]:
        if not self._enabled:
            return None
        rows, state_counts = self._build_rows()
        if not rows:
            return [{
                "component": "div",
                "text": "未从 qBittorrent 获取到任何种子，请确认下载器已连接且本插件已启用。",
                "props": {"class": "text-center"},
            }]
        return self._render(rows, state_counts)

    # ------------------------------------------------------------------ 下载器
    @staticmethod
    def _get_qb_services() -> Dict[str, Any]:
        helper = DownloaderHelper()
        services = helper.get_services()
        if not services:
            logger.warning(f"{LOG_TAG}未获取到任何下载器实例")
            return {}
        result: Dict[str, Any] = {}
        for name, info in services.items():
            try:
                if not helper.is_downloader(service_type="qbittorrent", service=info):
                    continue
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}判断下载器类型失败 [{name}]：{err}")
                continue
            if not getattr(info, "instance", None):
                continue
            try:
                if info.instance.is_inactive():
                    continue
            except Exception:  # pragma: no cover
                continue
            result[name] = info
        return result

    # ------------------------------------------------------------------ qB 种子字段（dict）
    @staticmethod
    def _t_hash(t: Any) -> str:
        return str(t.get("hash") or "").strip() if isinstance(t, dict) else ""

    @staticmethod
    def _t_name(t: Any) -> str:
        return str(t.get("name") or "") if isinstance(t, dict) else ""

    @staticmethod
    def _t_state(t: Any) -> str:
        return str(t.get("state") or "").strip() if isinstance(t, dict) else ""

    @staticmethod
    def _t_tags(t: Any) -> List[str]:
        if not isinstance(t, dict):
            return []
        return [s.strip() for s in str(t.get("tags") or "").split(",") if s.strip()]

    @staticmethod
    def _t_ratio(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("ratio") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_seeding_time(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("seeding_time") or t.get("time_active") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_size(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("size") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_progress(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            p = float(t.get("progress") or 0)
        except (TypeError, ValueError):
            return 0.0
        return p * 100 if p <= 1 else p

    # ------------------------------------------------------------------ 整理记录
    @staticmethod
    def _batch_history(hashes: List[str]) -> Dict[str, Dict[str, Any]]:
        """按 hash 批量查整理记录，补全海报/片名/来源站（只读 TransferHistoryOper）。"""
        hashes = [h for h in dict.fromkeys(hashes) if h]
        if not hashes:
            return {}
        try:
            from app.db.oper.transferhistory import TransferHistoryOper
            oper = TransferHistoryOper()
            out: Dict[str, Dict[str, Any]] = {}
            for h in hashes:
                rows = oper.list_by_hash(h) or []
                for rec in rows:
                    out[h] = {
                        "image": getattr(rec, "image", None),
                        "title": getattr(rec, "title", None),
                        "type": getattr(rec, "type", None),
                        "seasons": getattr(rec, "seasons", None),
                        "episodes": getattr(rec, "episodes", None),
                        "year": getattr(rec, "year", None),
                        "site": getattr(rec, "torrent_site", None)
                        or getattr(rec, "site_name", None),
                        "status": bool(getattr(rec, "status", None)),
                    }
                    break  # 取第一条即可
            return out
        except Exception as err:  # pragma: no cover
            logger.warning(f"{LOG_TAG}查询整理记录失败：{err}")
            return {}

    # ------------------------------------------------------------------ 数据组装
    def _build_rows(self):
        services = self._get_qb_services()
        if not services:
            return [], {}
        all_torrents: List[Any] = []
        for svc_name, svc in services.items():
            try:
                result = svc.instance.get_torrents()
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}获取下载器[{svc_name}]种子失败：{err}")
                continue
            # Qbittorrent.get_torrents() 返回 (种子列表, 错误标志) 二元组，必须拆包；
            # 旧代码直接 extend 把 (list, bool) 当成两个元素塞进列表，导致每个种子
            # 都不是 dict、hash 全为空、整张表被跳过，最终只显示“未获取到任何种子”。
            if isinstance(result, tuple) and len(result) == 2:
                torrents, error = result
            else:
                torrents, error = (result or []), False
            if error:
                logger.warning(f"{LOG_TAG}下载器[{svc_name}]未连接或返回错误，已跳过")
                continue
            all_torrents.extend(torrents or [])
        if not all_torrents:
            return [], {}

        hashes = [self._t_hash(t) for t in all_torrents]
        history = self._batch_history(hashes)

        rows: List[Dict[str, Any]] = []
        state_counts: Dict[str, int] = {}
        for t in all_torrents:
            h = self._t_hash(t)
            if not h:
                continue
            state = self._t_state(t)
            tags = self._t_tags(t)
            is_mp = self._MP_TAG in [x.upper() for x in tags]
            # 过滤：隐藏下载中（原生已展示）
            if self._hide_downloading and (state in self._DOWNLOADING_STATES or "DL" in state):
                continue
            # 过滤：仅 MP 管理种子
            if self._only_mp_tag and not is_mp:
                continue
            rec = history.get(h) or {}
            poster = rec.get("image")
            title = rec.get("title") or self._t_name(t)
            rows.append({
                "hash": h,
                "poster": poster,
                "title": title,
                "state": state,
                "progress": self._t_progress(t),
                "size": self._fmt_size(self._t_size(t)),
                "ratio": self._t_ratio(t),
                "seeding": self._fmt_duration(self._t_seeding_time(t)),
                "tags": ", ".join(tags) if tags else "",
                "site": rec.get("site") or "",
                "is_mp": is_mp,
            })
            state_counts[state] = state_counts.get(state, 0) + 1

        # 排序：做种/暂停优先，下载中靠后
        rows.sort(key=lambda r: (
            0 if ("UP" in r["state"] or "uploading" in r["state"]) else 1,
            r["state"], r["title"],
        ))
        return rows, state_counts

    # ------------------------------------------------------------------ 格式化
    @staticmethod
    def _fmt_size(size: float) -> str:
        if size >= 1e9:
            return f"{size / 1e9:.2f} GB"
        if size >= 1e6:
            return f"{size / 1e6:.0f} MB"
        if size >= 1e3:
            return f"{size / 1e3:.0f} KB"
        return f"{size:.0f} B"

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        sec = int(sec)
        if sec <= 0:
            return "-"
        d, rem = divmod(sec, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}天{h}时"
        if h:
            return f"{h}时{m}分"
        return f"{m}分"

    # ------------------------------------------------------------------ 渲染
    def _render(self, rows: List[Dict[str, Any]], state_counts: Dict[str, int]) -> List[dict]:
        cards = [self._stat_card(str(len(rows)), "合计种子")]
        for st, c in sorted(state_counts.items(), key=lambda x: -x[1]):
            cards.append(self._stat_card(str(c), st))
        header = {"component": "VRow", "content": cards}
        thead = {
            "component": "thead",
            "content": [
                self._th("海报"), self._th("名称/片名"), self._th("状态"),
                self._th("进度"), self._th("大小"), self._th("分享率"),
                self._th("做种时长"), self._th("标签"), self._th("来源站"), self._th("MP管理"),
            ],
        }
        trs = [self._row(r) for r in rows]
        table = {
            "component": "VCol",
            "props": {"cols": 12},
            "content": [{
                "component": "VTable",
                "props": {"hover": True, "density": "compact"},
                "content": [thead, {"component": "tbody", "content": trs}],
            }],
        }
        return [header, table]

    @staticmethod
    def _th(text: str) -> dict:
        return {"component": "th", "props": {"class": "text-start ps-4"}, "text": text}

    @staticmethod
    def _stat_card(value: str, label: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "sm": 3, "md": 2},
            "content": [{
                "component": "VCard",
                "props": {"class": "text-center pa-2"},
                "content": [
                    {"component": "VCardText", "props": {"class": "text-h6"}, "text": value},
                    {"component": "VCardText", "props": {"class": "text-caption"}, "text": label},
                ],
            }],
        }

    def _row(self, r: Dict[str, Any]) -> dict:
        if r["poster"]:
            poster_cell = {"component": "td", "content": [
                {"component": "img", "props": {
                    "src": r["poster"],
                    "style": "height:72px;border-radius:4px;object-fit:cover;",
                }}
            ]}
        else:
            poster_cell = {"component": "td", "text": "无海报"}
        mp_badge = "是" if r["is_mp"] else "否"
        return {
            "component": "tr",
            "props": {"class": "text-sm"},
            "content": [
                poster_cell,
                {"component": "td", "text": r["title"]},
                {"component": "td", "text": r["state"]},
                {"component": "td", "text": f"{r['progress']:.1f}%"},
                {"component": "td", "text": r["size"]},
                {"component": "td", "text": f"{r['ratio']:.2f}"},
                {"component": "td", "text": r["seeding"]},
                {"component": "td", "text": r["tags"]},
                {"component": "td", "text": r["site"]},
                {"component": "td", "props": {
                    "class": "text-success" if r["is_mp"] else "text-medium-emphasis",
                }, "text": mp_badge},
            ],
        }
