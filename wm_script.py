import argparse
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib3.util.retry import Retry

import requests
from tqdm import tqdm
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter

# 全局控制变量
should_stop = False
VALID_LINKS = []
OUTPUT_FILE = "valid_links.txt"

class TimeoutGuard:
    """时间守护系统"""
    def __init__(self, max_runtime, resume_file):
        self.start_time = time.time()
        self.max_runtime = max_runtime
        self.resume_file = resume_file
        self.last_progress = self.load_progress()
        
    def load_progress(self):
        """加载断点进度"""
        if os.path.exists(self.resume_file):
            with open(self.resume_file) as f:
                return f.read().strip()
        return None
    
    def save_progress(self, current_date):
        """保存进度"""
        with open(self.resume_file, 'w') as f:
            f.write(current_date.strftime('%Y%m%d'))
            
    def remaining_time(self):
        return self.max_runtime - (time.time() - self.start_time)
    
    def check_timeout(self):
        return self.remaining_time() < 300  # 剩余5分钟时触发安全停止

def signal_handler(sig, frame):
    global should_stop
    print("\n! 检测到中断信号，启动安全停止...")
    should_stop = True

signal.signal(signal.SIGINT, signal_handler)

def generate_wm_url(filehead, time_str):
    """生成动态WM链接"""
    domain = os.getenv('WM_DOMAIN')
    path = os.getenv('WM_PATH')
    return f"https://{domain}{path}{filehead}{time_str}_0.opt"

def generate_time_numbers(start_time, end_time):
    """生成有效时间序列"""
    start_sec = int(start_time[0:2])*3600 + int(start_time[2:4])*60 + int(start_time[4:6])
    end_sec = int(end_time[0:2])*3600 + int(end_time[2:4])*60 + int(end_time[4:6])
    
    if start_sec > end_sec:
        raise ValueError("每日结束时间不能早于开始时间")
    
    time_numbers = []
    for sec in range(start_sec, end_sec + 1):
        hour = sec // 3600
        minute = (sec % 3600) // 60
        second = sec % 60
        time_numbers.append(f"{hour:02d}{minute:02d}{second:02d}")
    return time_numbers

def create_retry_session():
    """创建带智能重试的会话"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=tuple(range(500, 600)),
        allowed_methods=['HEAD']
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=200,
        pool_maxsize=400,
        pool_block=True
    )
    session.mount('https://', adapter)
    return session

def check_wm_link(filehead, time_str, session):
    """验证WM链接有效性"""
    global should_stop
    if should_stop:
        return False
    
    url = generate_wm_url(filehead, time_str)
    try:
        response = session.head(url, timeout=5)
        if response.status_code == 200:
            return url
        return None
    except requests.exceptions.RequestException:
        return None

def process_date_range(args, timeout_guard):
    """处理指定日期范围"""
    session = create_retry_session()
    current_date = datetime.strptime(args.start_date, "%Y%m%d")
    end_date = datetime.strptime(args.end_date, "%Y%m%d")
    
    # 断点续传
    if timeout_guard.last_progress:
        resume_date = datetime.strptime(timeout_guard.last_progress, "%Y%m%d")
        if resume_date > current_date:
            current_date = resume_date
            print(f"从断点恢复: {current_date.strftime('%Y%m%d')}")

    while current_date <= end_date and not should_stop:
        if timeout_guard.check_timeout():
            print("\n🕒 接近时间限制，保存进度...")
            timeout_guard.save_progress(current_date)
            break
            
        filehead = f"{args.base}_{current_date.strftime('%Y%m%d')}"
        time_numbers = generate_time_numbers(args.start_time, args.end_time)
        
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(check_wm_link, filehead, tn, session): tn for tn in time_numbers}
            with tqdm(total=len(futures), desc=f"扫描 {current_date.strftime('%Y%m%d')}", leave=False) as pbar:
                for future in as_completed(futures):
                    if should_stop or timeout_guard.check_timeout():
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    result = future.result()
                    if result:
                        VALID_LINKS.append(result)
                    pbar.update(1)
        
        current_date += timedelta(days=1)
    
    return current_date

def main():
    parser = argparse.ArgumentParser(description='WM链接扫描器')
    parser.add_argument('--base', required=True, help='文件基础标识')
    parser.add_argument('--start_date', required=True, help='开始日期')
    parser.add_argument('--end_date', required=True, help='结束日期')
    parser.add_argument('--start_time', default='000000', help='每日起始时间')
    parser.add_argument('--end_time', default='235959', help='每日结束时间')
    parser.add_argument('--workers', type=int, default=50, help='并发线程数')
    parser.add_argument('--timeout', type=int, default=19800, help='最大运行时间(秒)')
    parser.add_argument('--resume-file', default='progress.log', help='断点记录文件')
    args = parser.parse_args()

    # 初始化时间守卫
    timeout_guard = TimeoutGuard(args.timeout, args.resume_file)
    
    try:
        last_processed = process_date_range(args, timeout_guard)
        if last_processed <= datetime.strptime(args.end_date, "%Y%m%d"):
            print(f"\n⏳ 部分完成，最后处理日期: {last_processed.strftime('%Y%m%d')}")
        else:
            print("\n✅ 扫描全部完成")
    except Exception as e:
        print(f"\n❌ 发生错误: {str(e)}")
        sys.exit(1)
    finally:
        with open(OUTPUT_FILE, 'w') as f:
            f.write("\n".join(VALID_LINKS))
        print(f"生成结果文件: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
