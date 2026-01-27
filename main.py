import os
import json
import time
import asyncio
import copy
import tempfile
from datetime import datetime
from typing import Dict, Any, Tuple
from pathlib import Path

from astrbot.api.all import Context, AstrMessageEvent, Star
from astrbot.api.event import filter
from astrbot.api import logger
from astrbot.api.star import StarTools

@register("astrbot_plugin_chatmaster", "ChatMaster", "活跃度监控插件", "2.1.1")
class ChatMasterPlugin(Star):
    SAVE_INTERVAL = 300       # 自动保存间隔
    CHECK_INTERVAL = 60       # 检查循环间隔
    CLEANUP_INTERVAL = 86400  # 强制清理间隔
    MAX_RETRIES = 3           # 推送重试次数
    CLEANUP_DAYS = 90         # 僵尸数据阈值
    MAX_DISPLAY_COUNT = 50    # 单条消息最大显示人数
    SEND_TIMEOUT = 15.0       # 推送超时 (秒)

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_changed = False 
        self.last_save_time = time.time()
        self.last_cleanup_time = time.time()
        
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_chatmaster")
        self.data_file = self.data_dir / "data.json"
        
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.data = self.load_data()
        
        self.nickname_cache = {}
        self.monitored_groups_set = set()
        self.exception_groups_set = set()
        self.enable_whitelist_global = True
        self.enable_mapping = True
        
        self.group_event_cache: Dict[str, AstrMessageEvent] = {}
        
        # 调度器状态锁 (防止同一分钟重复触发)
        self.last_processed_minute = -1
        
        self.refresh_config_cache()
        self.push_time_h, self.push_time_m = self._parse_push_time()
        
        server_time = datetime.now().strftime("%H:%M")
        logger.info(f"ChatMaster v2.1.1 已加载 (Removed Daily Limit)。")
        logger.info(f" -> 服务器时间: {server_time}")
        logger.info(f" -> 设定推送时间: {self.push_time_h:02d}:{self.push_time_m:02d}")

        self.cleanup_task = asyncio.create_task(self._cleanup_old_data_async())
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def _parse_push_time(self) -> Tuple[int, int]:
        push_time_str = self.config.get("push_time", "09:00")
        push_time_str = str(push_time_str).replace("：", ":")
        try:
            t = datetime.strptime(push_time_str, "%H:%M")
            return t.hour, t.minute
        except ValueError:
            logger.error(f"ChatMaster 配置错误: 推送时间 '{push_time_str}' 格式无效。已重置为 09:00")
            return 9, 0

    def refresh_config_cache(self):
        self.enable_whitelist_global = self.config.get("enable_whitelist", True)
        self.enable_mapping = self.config.get("enable_nickname_mapping", True)
        
        raw_groups = self.config.get("monitored_groups", [])
        self.monitored_groups_set = set(str(g) for g in raw_groups)
        
        raw_exceptions = self.config.get("whitelist_exception_groups", [])
        self.exception_groups_set = set(str(g) for g in raw_exceptions)

        mapping = {}
        raw_list = self.config.get("nickname_mapping", [])
        if raw_list:
            for item in raw_list:
                try:
                    if isinstance(item, dict):
                        for k, v in item.items():
                            mapping[str(k).strip()] = str(v).strip()
                    else:
                        item_str = str(item)
                        parts = []
                        if ":" in item_str:
                            parts = item_str.split(":", 1)
                        elif "：" in item_str:
                            parts = item_str.split("：", 1)
                        
                        if len(parts) == 2:
                            qq = parts[0].strip()
                            name = parts[1].strip()
                            mapping[qq] = name
                except Exception:
                    continue
        self.nickname_cache = mapping

    def _is_group_whitelist_mode(self, group_id: str) -> bool:
        mode = self.enable_whitelist_global
        if group_id in self.exception_groups_set:
            mode = not mode
        return mode

    def load_data(self) -> Dict[str, Any]:
        default_data = {"global_last_run_date": "", "groups": {}}
        if not self.data_file.exists():
            return default_data
        try:
            content = self.data_file.read_text(encoding='utf-8').strip()
            if not content:
                return default_data
            loaded = json.loads(content)
            if not isinstance(loaded, dict):
                return default_data
            if "groups" not in loaded or not isinstance(loaded["groups"], dict):
                loaded["groups"] = {}
            if "global_last_run_date" not in loaded:
                loaded["global_last_run_date"] = ""
            return loaded
        except Exception as e:
            logger.error(f"ChatMaster 加载数据失败: {e}，使用空数据。")
            return default_data

    def _save_data_atomic(self, data_snapshot: Dict[str, Any]):
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(dir=self.data_dir, text=False)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data_snapshot, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.data_file)
        except Exception as e:
            logger.error(f"ChatMaster 保存数据失败: {e}")
            if temp_path and os.path.exists(temp_path):
                try: os.remove(temp_path)
                except: pass

    async def save_data(self):
        if not self.data_changed:
            return
        try:
            data_snapshot = self.data.copy()
            await asyncio.to_thread(self._save_data_atomic, data_snapshot)
            self.data_changed = False
            self.last_save_time = time.time()
        except Exception as e:
            logger.error(f"ChatMaster 异步保存出错: {e}")

    async def _cleanup_old_data_async(self):
        if not self.data.get("groups"):
            return
        cutoff_time = time.time() - (self.CLEANUP_DAYS * 24 * 3600)
        removed_count = 0
        groups_to_check = list(self.data["groups"].keys())
        
        for i, group_id in enumerate(groups_to_check):
            if i % 10 == 0: await asyncio.sleep(0)
            
            group_data = self.data["groups"].get(group_id)
            if group_data is None: continue
                
            users_to_remove = [uid for uid, ts in group_data.items() if ts < cutoff_time]
            for uid in users_to_remove:
                del group_data[uid]
                removed_count += 1
                
        if removed_count > 0:
            logger.info(f"ChatMaster: 自动清理了 {removed_count} 条过期数据。")
            self.data_changed = True

    async def terminate(self):
        if self.scheduler_task: self.scheduler_task.cancel()
        if hasattr(self, 'cleanup_task') and self.cleanup_task: self.cleanup_task.cancel()
        try:
            self._save_data_atomic(self.data)
            logger.info("ChatMaster 插件已停止，数据已保存。")
        except Exception as e:
            logger.error(f"ChatMaster 停止时保存失败: {e}")

    def _get_display_name(self, user_id: str) -> str:
        if self.enable_mapping and user_id in self.nickname_cache:
            return self.nickname_cache[user_id]
        return f"用户{user_id}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id or not message_obj.sender:
            return

        group_id = str(message_obj.group_id)
        user_id = str(message_obj.sender.user_id)
        
        self.group_event_cache[group_id] = event
        
        if group_id not in self.monitored_groups_set:
            return

        use_whitelist = self._is_group_whitelist_mode(group_id)
        if use_whitelist and user_id not in self.nickname_cache:
            return 
        
        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {}

        self.data["groups"][group_id][user_id] = time.time()
        self.data_changed = True 

    @filter.command("聊天检测")
    async def manual_check(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id:
            yield event.plain_result("🚫 请在群聊中使用此命令。")
            return

        group_id = str(message_obj.group_id)
        
        if group_id not in self.data["groups"] or not self.data["groups"][group_id]:
            yield event.plain_result(f"📭 群 ({group_id}) 暂无监控数据。")
            return

        group_data = self.data["groups"][group_id]
        msg_lines = [f"📊 群 ({group_id}) 活跃度数据概览："]
        
        now = time.time()
        count = 0
        
        self.refresh_config_cache()
        use_whitelist = self._is_group_whitelist_mode(group_id)
        mode_str = "白名单模式" if use_whitelist else "全员监控模式"
        msg_lines.append(f"当前模式: {mode_str}")
        
        user_items = list(group_data.items())
        
        for i, (user_id, last_seen_ts) in enumerate(user_items):
            if i % 50 == 0: await asyncio.sleep(0)

            if use_whitelist and user_id not in self.nickname_cache:
                continue
            
            if count >= self.MAX_DISPLAY_COUNT:
                msg_lines.append(f"\n⚠️ (名单过长，系统截断前 {self.MAX_DISPLAY_COUNT} 位显示)")
                break

            nickname = self._get_display_name(user_id)
            last_seen_dt = datetime.fromtimestamp(last_seen_ts)
            last_seen_str = last_seen_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            diff_seconds = now - last_seen_ts
            days = int(diff_seconds // 86400)
            
            status_emoji = "🟢" if days < 1 else "🔴"
            msg_lines.append(f"{status_emoji} {nickname} | 未发言: {days}天 | 最后: {last_seen_str}")
            count += 1

        msg_lines.append(f"\n共记录 {count} 人。")
        yield event.plain_result("\n".join(msg_lines))

    @filter.command("重置检测")
    async def reset_check_status(self, event: AstrMessageEvent):
        # 虽然逻辑上去掉了每日一次的限制，但这个指令保留着也没坏处，
        # 可以作为"强制重置所有状态"的备用手段
        self.last_processed_minute = -1
        yield event.plain_result("✅ 调度器状态已重置，下一分钟即可再次触发。")

    async def scheduler_loop(self):
        while True:
            try:
                self.refresh_config_cache()
                target_h, target_m = self._parse_push_time()
                await self.check_schedule(target_h, target_m)
                
                if time.time() - self.last_cleanup_time > self.CLEANUP_INTERVAL:
                    await self._cleanup_old_data_async()
                    self.last_cleanup_time = time.time()

                if self.data_changed and (time.time() - self.last_save_time > self.SAVE_INTERVAL):
                    await self.save_data()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ChatMaster 调度出错: {e}")
            
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def check_schedule(self, target_h: int, target_m: int):
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        target_minutes = target_h * 60 + target_m
        
        # 1. 状态锁：防止同一分钟重复执行 (防重入)
        if current_minutes == self.last_processed_minute:
            return
        
        # 2. 核心修改：移除"日期锁"，改为"精准时刻触发"
        # 只要当前时间等于设定时间，就触发。
        if current_minutes == target_minutes:
            self.last_processed_minute = current_minutes # 更新锁
            
            logger.info(f"ChatMaster: ⏰ 到达推送时间 {target_h:02d}:{target_m:02d}，执行任务...")
            await self.run_inspection(send_message=True)
            
            # 记录一下运行时间 (仅作参考)
            self.data["global_last_run_date"] = now.strftime("%Y-%m-%d")
            self.data_changed = True
            await self.save_data()

    async def run_inspection(self, send_message: bool = True):
        timeout_days_cfg = float(self.config.get("timeout_days", 1.0))
        timeout_seconds = timeout_days_cfg * 24 * 3600
        template = self.config.get("alert_template", "“{nickname}”已经“{days}”天没发言了")
        now_ts = time.time()

        if not self.monitored_groups_set:
            return

        for group_id in self.monitored_groups_set:
            try:
                group_data = self.data["groups"].get(group_id, {})
                use_whitelist = self._is_group_whitelist_mode(group_id)
                mode_str = "白名单" if use_whitelist else "全员"
                
                log_lines = []
                log_lines.append(f"ChatMaster: 检测群 {group_id} [{mode_str}]...")

                if not group_data:
                    log_lines.append("  -> 暂无活跃数据。")
                    logger.info("\n".join(log_lines))
                    continue

                msg_list = []
                active_names = []
                inactive_names = []
                
                user_items = list(group_data.items())
                count = 0 
                for i, (user_id, last_seen_ts) in enumerate(user_items):
                    if i % 50 == 0: await asyncio.sleep(0)

                    if use_whitelist and user_id not in self.nickname_cache:
                        continue
                    
                    nickname = self._get_display_name(user_id)
                    time_diff = now_ts - last_seen_ts
                    
                    if time_diff >= timeout_seconds:
                        days_silent = int(time_diff // 86400)
                        last_seen_str = datetime.fromtimestamp(last_seen_ts).strftime('%Y-%m-%d %H:%M:%S')
                        
                        if count < self.MAX_DISPLAY_COUNT:
                            line = template.format(
                                nickname=nickname, 
                                days=days_silent, 
                                last_seen=last_seen_str
                            )
                            msg_list.append(line)
                        
                        inactive_names.append(f"{nickname}({days_silent}天)")
                    else:
                        active_names.append(nickname)
                    
                    count += 1
                
                if count > self.MAX_DISPLAY_COUNT and len(msg_list) >= self.MAX_DISPLAY_COUNT:
                     msg_list.append(f"\n⚠️ (名单过长，系统截断前 {self.MAX_DISPLAY_COUNT} 位显示)")

                if active_names:
                    log_lines.append(f"  🟢 活跃人员 ({len(active_names)}): {', '.join(active_names[:100])}{'...' if len(active_names)>100 else ''}")
                if inactive_names:
                    log_lines.append(f"  🔴 潜水人员 ({len(inactive_names)}): {', '.join(inactive_names[:100])}{'...' if len(inactive_names)>100 else ''}")

                if msg_list:
                    if send_message:
                        log_lines.append(f"  -> 结论: ❌ 发现 {len(inactive_names)} 人潜水，正在推送...")
                        logger.info("\n".join(log_lines))
                        
                        final_msg = "\n".join(msg_list)
                        
                        success = False
                        # 方案A：标准推送
                        if not success:
                            try:
                                await asyncio.wait_for(
                                    self.context.send_message(
                                        group_id=group_id, 
                                        message_str=f"📢 潜水员日报：\n{final_msg}"
                                    ),
                                    timeout=self.SEND_TIMEOUT
                                )
                                success = True
                            except Exception as e:
                                log_lines.append(f"  -> ⚠️ 标准推送失败 ({e})，尝试事件缓存...")
                        
                        # 方案B：事件缓存
                        if not success:
                            cached_event = self.group_event_cache.get(group_id)
                            if cached_event:
                                try:
                                    await asyncio.wait_for(
                                        self.context.send_message(
                                            event=cached_event,
                                            message_str=f"📢 潜水员日报：\n{final_msg}"
                                        ),
                                        timeout=self.SEND_TIMEOUT
                                    )
                                    success = True
                                except Exception as e:
                                    logger.error(f"ChatMaster: 群 {group_id} 缓存推送也失败: {e}")
                            else:
                                logger.warning(f"ChatMaster: 群 {group_id} 无缓存，且标准推送失败。")

                    else:
                        log_lines.append(f"  -> 结论: ⚠️ 发现潜水人员，但设置为不发送。")
                        logger.info("\n".join(log_lines))
                else:
                    log_lines.append("  -> 结论: ✅ 全员活跃 (无需推送)。")
                    logger.info("\n".join(log_lines))

            except Exception as e:
                logger.error(f"ChatMaster: 处理群 {group_id} 错误: {e}")
                continue
