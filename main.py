import json
import time
import asyncio
import copy
from datetime import datetime
from typing import Dict, Any, Tuple

from astrbot.api.all import Context, AstrMessageEvent, Star, register
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api.event import filter as astr_filter  # 修正导入方式

@register("astrbot_plugin_chatmaster", "ChatMaster", "活跃度监控插件", "2.0.1")
class ChatMasterPlugin(Star):
    SAVE_INTERVAL = 300
    CHECK_INTERVAL = 60
    MAX_RETRIES = 3

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_changed = False 
        self.last_save_time = time.time()
        
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_chatmaster")
        self.data_file = self.data_dir / "data.json"
        
        self.data = self.load_data()
        
        self.nickname_cache = {}
        self.monitored_groups_set = set()
        self.exception_groups_set = set()
        self.enable_whitelist_global = True
        self.enable_mapping = True
        
        self.refresh_config_cache()
        self._parse_push_time()
        
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def _parse_push_time(self) -> Tuple[int, int]:
        push_time_str = self.config.get("push_time", "09:00")
        push_time_str = push_time_str.replace("：", ":")
        try:
            parts = push_time_str.split(':')
            if len(parts) >= 2:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h < 24 and 0 <= m < 60:
                    self.push_time_h, self.push_time_m = h, m
                    return h, m
            raise ValueError("格式错误")
        except Exception as e:
            logger.error(f"ChatMaster 配置错误: 推送时间 '{push_time_str}' 无效 ({e})。已重置为 09:00")
            self.push_time_h, self.push_time_m = 9, 0
            return 9, 0

    def refresh_config_cache(self):
        """刷新配置缓存"""
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
        self.nickname_cache = mapping

    def _is_group_whitelist_mode(self, group_id: str) -> bool:
        """判断指定群是否开启了白名单模式"""
        mode = self.enable_whitelist_global
        if group_id in self.exception_groups_set:
            mode = not mode
        return mode

    def load_data(self) -> Dict[str, Any]:
        default_data = {"global_last_run_date": "", "groups": {}}
        if not self.data_file.exists():
            return default_data
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return default_data
                loaded = json.loads(content)
                if not isinstance(loaded, dict):
                    return default_data
                if "groups" not in loaded:
                    loaded["groups"] = {}
                if "global_last_run_date" not in loaded:
                    loaded["global_last_run_date"] = ""
                return loaded
        except Exception as e:
            logger.error(f"ChatMaster 加载数据失败: {e}，使用空数据。")
            return default_data

    def _save_data_sync(self, data_snapshot: Dict[str, Any]):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data_snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"ChatMaster 保存数据失败: {e}")

    async def save_data(self):
        if not self.data_changed:
            return
        try:
            data_copy = copy.deepcopy(self.data)
            await asyncio.to_thread(self._save_data_sync, data_copy)
            self.data_changed = False
            self.last_save_time = time.time()
        except Exception as e:
            logger.error(f"ChatMaster 异步保存出错: {e}")

    def terminate(self):
        if self.scheduler_task:
            self.scheduler_task.cancel()
        self._save_data_sync(self.data)
        logger.info("ChatMaster 插件已停止，数据已保存。")

    def _get_display_name(self, user_id: str) -> str:
        if self.enable_mapping and user_id in self.nickname_cache:
            return self.nickname_cache[user_id]
        return f"用户{user_id}"

    # 修正调用方式：使用 astr_filter.EventMessageType
    @astr_filter.event_message_type(astr_filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id:
            return

        group_id = str(message_obj.group_id)
        user_id = str(message_obj.sender.user_id)
        
        if group_id not in self.monitored_groups_set:
            return

        use_whitelist = self._is_group_whitelist_mode(group_id)
        
        if use_whitelist and user_id not in self.nickname_cache:
            return 
        
        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {}

        self.data["groups"][group_id][user_id] = time.time()
        self.data_changed = True 

    @astr_filter.command("聊天检测")
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
        
        for user_id, last_seen_ts in group_data.items():
            if use_whitelist and user_id not in self.nickname_cache:
                continue
                
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

    async def scheduler_loop(self):
        while True:
            try:
                self.refresh_config_cache()
                self._parse_push_time()
                await self.check_schedule()
                
                if self.data_changed and (time.time() - self.last_save_time > self.SAVE_INTERVAL):
                    await self.save_data()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ChatMaster 调度出错: {e}")
            
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def check_schedule(self):
        now = datetime.now()
        today_date_str = now.strftime("%Y-%m-%d")
        
        target_h, target_m = self.push_time_h, self.push_time_m

        is_time_up = (now.hour > target_h) or (now.hour == target_h and now.minute >= target_m)
        last_run = self.data.get("global_last_run_date", "")
        
        if is_time_up and last_run != today_date_str:
            logger.info(f"ChatMaster: 到达设定时间 {target_h:02d}:{target_m:02d}，触发每日检测...")
            
            self.data["global_last_run_date"] = today_date_str
            self.data_changed = True
            await self.save_data()
            
            await self.run_inspection()

    async def run_inspection(self):
        timeout_days_cfg = float(self.config.get("timeout_days", 1.0))
        timeout_seconds = timeout_days_cfg * 24 * 3600
        template = self.config.get("alert_template", "“{nickname}”已经“{days}”天没发言了")
        now_ts = time.time()

        logger.info(f"ChatMaster: === 开始执行活跃度检测 (阈值: {timeout_days_cfg}天) ===")

        for group_id in self.monitored_groups_set:
            try:
                group_data = self.data["groups"].get(group_id, {})
                if not group_data:
                    continue

                use_whitelist = self._is_group_whitelist_mode(group_id)
                msg_list = []
                
                for user_id, last_seen_ts in group_data.items():
                    if use_whitelist and user_id not in self.nickname_cache:
                        continue
                    
                    time_diff = now_ts - last_seen_ts
                    
                    if time_diff >= timeout_seconds:
                        nickname = self._get_display_name(user_id)
                        days_silent = int(time_diff // 86400)
                        last_seen_str = datetime.fromtimestamp(last_seen_ts).strftime('%Y-%m-%d %H:%M:%S')
                        
                        line = template.format(
                            nickname=nickname, 
                            days=days_silent, 
                            last_seen=last_seen_str
                        )
                        msg_list.append(line)
                        logger.info(f"ChatMaster:   -> 发现潜水员: {nickname} (未发言 {days_silent} 天)")
                
                if msg_list:
                    logger.info(f"ChatMaster: -> 群 {group_id} 结果: 需推送。共发现 {len(msg_list)} 人。")
                    final_msg = "\n".join(msg_list)
                    
                    for attempt in range(self.MAX_RETRIES):
                        try:
                            await self.context.send_message(
                                target_group_id=group_id, 
                                message_str=f"📢 潜水员日报：\n{final_msg}"
                            )
                            break 
                        except Exception as e:
                            if attempt == self.MAX_RETRIES - 1:
                                logger.error(f"ChatMaster: 群 {group_id} 推送失败，放弃: {e}")
                            else:
                                await asyncio.sleep(1)
                                
                    await asyncio.sleep(2)
                else:
                    logger.info(f"ChatMaster: -> 群 {group_id} 结果: 无需推送。")

            except Exception as e:
                logger.error(f"ChatMaster: 处理群 {group_id} 时发生错误: {e}")
                continue
