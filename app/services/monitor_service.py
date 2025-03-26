import re
import subprocess
import threading
import logging
import time
import psutil
from app.utils.network_utils import extract_host_from_rtmp, validate_rtmp_url
from app.services.stream_service import active_processes, process_lock
import pytz
from typing import Tuple

# 设置北京时区
beijing_tz = pytz.timezone('Asia/Shanghai')

logger = logging.getLogger('youtube_live')

def monitor_all_rtmp_connections():
    """监控所有活动任务的RTMP连接质量"""
    try:
        # 定义用于收集任务组的字典
        hosts_to_check = {}  # {host: [task_ids]}
    
        # 获取所有活动任务
        with process_lock:
            if not active_processes:
                return  # 没有活动任务，直接返回
            
            # 检查每个任务的连接状态和异常情况
            for task_id, info in list(active_processes.items()):
                # 检查进程是否仍在运行
                process = info.get('process')
                if not process or process.poll() is not None:
                    logger.warning(f'监控发现任务进程已停止 - task_id={task_id}, 返回码={process.poll() if process else "None"}')
                    continue  # 进程已结束，跳过
                
                # 检查进程是否僵尸状态（无法响应但仍占用PID）
                try:
                    import psutil
                    try:
                        proc = psutil.Process(process.pid)
                        proc_status = proc.status()
                        if proc_status == psutil.STATUS_ZOMBIE:
                            logger.warning(f'检测到僵尸进程 - task_id={task_id}, pid={process.pid}')
                            # 先尝试发送SIGTERM信号
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except:
                                # 如果SIGTERM无效，使用SIGKILL强制结束
                                proc.kill()
                            logger.info(f'已终止僵尸进程 - task_id={task_id}, pid={process.pid}')
                            
                            # 更新任务状态
                            from app.services.task_service import update_task_status
                            from datetime import datetime
                            update_task_status(task_id, {
                                'status': 'error',
                                'message': '任务已自动终止（僵尸进程）',
                                'end_time': datetime.now(beijing_tz).isoformat()
                            })
                            
                            # 从活动任务列表中移除
                            del active_processes[task_id]
                            continue
                    except psutil.NoSuchProcess:
                        logger.warning(f'进程不存在但仍在活动列表中 - task_id={task_id}, pid={process.pid}')
                        # 从活动任务列表中移除
                        del active_processes[task_id]
                        continue
                except Exception as e:
                    logger.error(f'检查进程状态失败 - task_id={task_id}: {str(e)}')
                
                # 按照主机名分组任务
                rtmp_url = info.get('rtmp_url', '')
                host = extract_host_from_rtmp(rtmp_url)
                
                if not host:
                    continue
                    
                if host not in hosts_to_check:
                    hosts_to_check[host] = []
                    
                hosts_to_check[host].append(task_id)
        
        # 没有需要检查的主机
        if not hosts_to_check:
            return
            
        logger.debug(f'开始检查 {len(hosts_to_check)} 个RTMP服务器的连接质量')
        
        # 检查每个主机
        for host, task_ids in hosts_to_check.items():
            try:
                # 执行ping测试
                result = subprocess.run(['ping', '-c', '3', host], capture_output=True, text=True, timeout=5)
                ping_output = result.stdout
                
                # 分析ping结果
                network_status = "未知"
                
                if result.returncode != 0:
                    network_status = '不稳定'
                    logger.warning(f'RTMP服务器连接不稳定 - host={host}, 影响 {len(task_ids)} 个任务')
                else:
                    # 分析ping延迟
                    avg_ping = 0
                    packet_loss = 0
                    
                    try:
                        # 提取丢包率
                        loss_match = re.search(r'(\d+)% packet loss', ping_output)
                        if loss_match:
                            packet_loss = int(loss_match.group(1))
                        
                        # 提取平均延迟
                        avg_match = re.search(r'min/avg/max/mdev = [\d.]+/([\d.]+)', ping_output)
                        if avg_match:
                            avg_ping = float(avg_match.group(1))
                    except:
                        pass
                        
                    # 根据延迟和丢包率评估连接质量
                    if packet_loss > 10 or avg_ping > 200:
                        network_status = f'不佳 (延迟:{avg_ping:.1f}ms, 丢包:{packet_loss}%)'
                        logger.warning(f'RTMP连接质量不佳 - host={host}, 平均延迟={avg_ping}ms, 丢包率={packet_loss}%, 影响 {len(task_ids)} 个任务')
                    else:
                        network_status = f'良好 (延迟:{avg_ping:.1f}ms, 丢包:{packet_loss}%)'
                        logger.info(f'RTMP连接质量良好 - host={host}, 平均延迟={avg_ping}ms, 丢包率={packet_loss}%')
                
                # 更新所有受影响任务的网络状态
                with process_lock:
                    for task_id in task_ids:
                        if task_id in active_processes:
                            active_processes[task_id]['network_status'] = network_status
            except Exception as e:
                logger.error(f'检查主机 {host} 的连接质量失败: {str(e)}')
                
    except Exception as e:
        logger.error(f'全局网络质量监控失败: {str(e)}')

class ResourceMonitor:
    """系统资源监控类"""
    
    def __init__(self):
        self.running = False
        self.monitoring_interval = 30  # 监控间隔（秒）
        self.system_monitor_thread = None
    
    def start_monitoring(self):
        """启动监控服务"""
        self.running = True
        logger.info("启动资源监控服务")
    
    def stop_monitoring(self):
        """停止监控服务"""
        self.running = False
        logger.info("停止资源监控服务")
        
        # 停止系统监控线程
        if self.system_monitor_thread and self.system_monitor_thread.is_alive():
            self.system_monitor_thread.join(timeout=5)
            logger.info("系统监控线程已停止")
    
    def run(self):
        """资源监控主循环"""
        try:
            logger.info("资源监控线程已启动")
            
            while self.running:
                try:
                    # 监控CPU和内存使用率
                    cpu_percent = psutil.cpu_percent(interval=1)
                    memory_info = psutil.virtual_memory()
                    
                    # 当CPU或内存使用率过高时记录警告
                    if cpu_percent > 80:
                        logger.warning(f"CPU使用率过高: {cpu_percent}%")
                        
                    if memory_info.percent > 85:
                        logger.warning(f"内存使用率过高: {memory_info.percent}%")
                        
                    # 仅在日志级别为DEBUG时才记录常规资源信息
                    if logger.level <= logging.DEBUG:
                        logger.debug(f"系统资源使用情况 - CPU: {cpu_percent}%, 内存: {memory_info.percent}%")
                    
                    # 检查并清理可能存在的僵尸进程
                    for proc in psutil.process_iter(['pid', 'name', 'status']):
                        try:
                            if proc.info['status'] == psutil.STATUS_ZOMBIE and proc.info['name'] == 'ffmpeg':
                                logger.warning(f"检测到僵尸进程: PID={proc.info['pid']}, 名称={proc.info['name']}")
                                # 不主动终止僵尸进程，只记录日志
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    
                    # 周期性调用RTMP网络监控
                    monitor_all_rtmp_connections()
                    
                except Exception as e:
                    logger.error(f"资源监控过程中发生错误: {str(e)}")
                
                # 监控间隔
                time.sleep(self.monitoring_interval)
                
        except Exception as e:
            logger.error(f"资源监控线程异常退出: {str(e)}")
        finally:
            logger.info("资源监控线程已结束") 