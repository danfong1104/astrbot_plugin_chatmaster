import os
import json
import time
import asyncio
from datetime import datetime
from astrbot.api.all import *
from astrbot.api.event import filter

@register("astrbot_plugin_chatmaster", "ChatMaster", "活跃度监控插件", "1.2.5")
class ChatMasterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        self.data_file = os.path.join(os.path.dirname(__file__), "data.json")
        self.data = self.load_data()
        
        asyncio.create_task(self.scheduler_loop())

    def load_data(self):
        default_data = {"global_last_run_date": "", "groups": {}}
        if not os.path.exists(self.data_file):
            return default_data
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if loaded and "global_last_run_date" not in loaded:
                    return {"global_last_run_date": "", "groups": loaded}
                return loaded
        except:
            return default_data

    def save_data(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.context.logger.error(f"ChatMaster 保存数据失败: {e}")

    def is_user_allowed(self, user_id):
        """检查用户是否在白名单（昵称映射表）中"""
        user_id_str = str(user_id)
        mapping_list = self.config.get("nickname_mapping", [])
        
        if not mapping_list:
            return False

        for item in mapping_list:
            item_str = str(item).replace("：", ":")
            if ":" in item_str:
                parts = item_str.split(":", 1)
                if len(parts) == 2:
                    qq_cfg = parts[0].strip()
                    if qq_cfg == user_id_str:
                        return True
        return False

    def get_nickname(self, user_id):
        """从配置列表 'QQ:昵称' 中解析昵称"""
        user_id_str = str(user_id)
        mapping_list = self.config.get("nickname_mapping", [])
        
        if not mapping_list:
            return f"用户{user_id_str}"

        for item in mapping_list:
            item_str = str(item).replace("：", ":")
            if ":" in item_str:
                parts = item_str.split(":", 1)
                if len(parts) == 2:
                    qq_cfg, name_cfg = parts
                    if qq_cfg.strip() == user_id_str:
                        return name_cfg.strip()
        
        return f"用户{user_id_str}"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id:
            return

        group_id = str(message_obj.group_id)
        user_id = str(message_obj.sender.user_id)
        
        # 1. 检查群是否在监控列表
        monitored_groups = self.config.get("monitored_groups", [])
        if monitored_groups and group_id not in monitored_groups:
            return

        # 2. 检查用户是否在昵称白名单里
        if not self.is_user_allowed(user_id):
            return 

        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {}

        self.data["groups"][group_id][user_id] = time.time()
        self.save_data()

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
        
        for user_id, last_seen_ts in group_data.items():
            if not self.is_user_allowed(user_id):
                continue
                
            nickname = self.get_nickname(user_id)
            last_seen_dt = datetime.fromtimestamp(last_seen_ts)
            last_seen_str = last_seen_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            diff_seconds = now - last_seen_ts
            days = int(diff_seconds // 86400)
            
            status_emoji = "🟢" if days < 1 else "🔴"
            msg_lines.append(f"{status_emoji} {nickname} | 未发言: {days}天 | 最后: {last_seen_str}")
            count += 1

        msg_lines.append(f"\n共记录 {count} 人（仅统计白名单用户）。")
        yield event.plain_result("\n".join(msg_lines))

    async def scheduler_loop(self):
        while True:
            try:
                await self.check_schedule()
            except Exception as e:
                self.context.logger.error(f"ChatMaster 调度出错: {e}")
            await asyncio.sleep(60)

    async def check_schedule(self):
        push_time_str = self.config.get("push_time", "09:00")
        
        now = datetime.now()
        today_date_str = now.strftime("%Y-%m-%d")
        
        try:
            target_h, target_m = map(int, push_time_str.split(':'))
        except:
            target_h, target_m = 9, 0

        is_time_up = (now.hour > target_h) or (now.hour == target_h and now.minute >= target_m)
        last_run = self.data.get("global_last_run_date", "")
        
        if is_time_up and last_run != today_date_str:
            # 这里的日志只在确实触发时打印一次
            self.context.logger.info(f"ChatMaster: 到达设定时间 {push_time_str}，触发每日检测...")
            await self.run_inspection()
            self.data["global_last_run_date"] = today_date_str
            self.save_data()

    async def run_inspection(self):
        monitored_groups = self.config.get("monitored_groups", [])
        timeout_days_cfg = float(self.config.get("timeout_days", 1.0))
        timeout_seconds = timeout_days_cfg * 24 * 3600
        template = self.config.get("alert_template", "“{nickname}”已经“{days}”天没发言了")
        now_ts = time.time()

        self.context.logger.info(f"ChatMaster: === 开始执行活跃度检测 (阈值: {timeout_days_cfg}天) ===")

        for group_id in monitored_groups:
            group_id = str(group_id)
            group_data = self.data["groups"].get(group_id, {})
            
            # 打印正在检测哪个群
            self.context.logger.info(f"ChatMaster: 正在检测群 {group_id} ...")

            if not group_data:
                self.context.logger.info(f"ChatMaster: -> 群 {group_id} 暂无数据，跳过。")
                continue

            msg_list = []
            checked_count = 0
            
            for user_id, last_seen_ts in group_data.items():
                if not self.is_user_allowed(user_id):
                    continue
                
                checked_count += 1
                time_diff = now_ts - last_seen_ts
                
                if time_diff >= timeout_seconds:
                    nickname = self.get_nickname(user_id)
                    days_silent = int(time_diff // 86400)
                    last_seen_str = datetime.fromtimestamp(last_seen_ts).strftime('%Y-%m-%d %H:%M:%S')
                    
                    line = template.format(
                        nickname=nickname, 
                        days=days_silent, 
                        last_seen=last_seen_str
                    )
                    msg_list.append(line)
                    # 打印单条命中日志
                    self.context.logger.info(f"ChatMaster:   -> 发现潜水员: {nickname} (未发言 {days_silent} 天)")
            
            if msg_list:
                self.context.logger.info(f"ChatMaster: -> 结果: 需推送。共发现 {len(msg_list)} 人。")
                final_msg = "\n".join(msg_list)
                await self.context.send_message(
                    target_group_id=group_id, 
                    message_str=f"📢 潜水员日报：\n{final_msg}"
                )
                await asyncio.sleep(2)
            else:
                self.context.logger.info(f"ChatMaster: -> 结果: 无需推送 (检测了 {checked_count} 个白名单用户，均活跃)。")
        
        self.context.logger.info(f"ChatMaster: === 检测结束 ===")