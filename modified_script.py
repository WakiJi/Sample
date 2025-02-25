import argparse
import signal
import sys
import time
import os
import base64
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

# ANSI颜色代码 (保留用于本地运行)
COLORS = {
    "success": 32,    # 绿色
    "error": 31,       # 红色
    "warning": 33,     # 黄色
    "info": 36         # 青色
}

def signal_handler(sig, frame):
    """处理键盘中断信号"""
    global should_stop
    print("\n! 检测到中断信号，正在停止...")
    should_stop = True

signal.signal(signal.SIGINT, signal_handler)

def validate_date(date_str):
    """验证日期格式和有效性"""
    try:
        return datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        raise ValueError(f"无效的日期格式: {date_str} (应为YYYYMMDD)")

def is_leap_year(year):
    """判断闰年"""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)

def validate_date_range(start_date, end_date):
    """验证日期范围有效性"""
    if start_date > end_date:
        raise ValueError("结束日期不能早于开始日期")
    
    # 二月天数校验
    current = start_date
    while current <= end_date:
        if current.month == 2:
            feb_days = 29 if is_leap_year(current.year) else 28
            if current.day > feb_days:
                raise ValueError(f"{current.year}年2月最多有{feb_days}天")
        current += timedelta(days=1)

def validate_time(time_str):
    """验证时间格式和有效性"""
    if len(time_str) != 6 or not time_str.isdigit():
        raise ValueError(f"无效的时间格式: {time_str} (应为HHMMSS)")
    
    hour = int(time_str[0:2])
    minute = int(time_str[2:4])
    second = int(time_str[4:6])
    
    if not (0 <= hour <= 23):
        raise ValueError(f"小时超出范围: {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"分钟无效: {minute}")
    if not (0 <= second <= 59):
        raise ValueError(f"秒数无效: {second}")
    
    return f"{hour:02d}{minute:02d}{second:02d}"


def print_status(tag, status, status_type="info"):
    """带颜色输出的状态打印 (CI环境自动禁用颜色)"""
    if os.getenv('CI') == 'true':
        tqdm.write(f"{tag}: {status}")
    else:
        color_code = COLORS.get(status_type, 37)
        message = f"\033[1;{color_code}m{tag}: {status}\033[0m"
        tqdm.write(message)

def generate_url(filehead, time_str):
    """生成动态URL"""
    domain = os.getenv('WM_DOMAIN')
    path = os.getenv('WM_PATH')
    return f"https://{domain}{path}{filehead}{time_str}_0.opt"

def create_retry_session():
    """创建带重试机制的会话"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=['HEAD']
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=50,
        pool_maxsize=100
    )
    session.mount('https://', adapter)
    return session

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

def check_link(filehead, time_str):
    """检查链接有效性"""
    global should_stop
    if should_stop:
        return False
    
    url = generate_url(filehead, time_str)
    tag = f"{filehead} {time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
    
    try:
        session = create_retry_session()
        response = session.head(url, timeout=8)
        
        if response.status_code == 200:
            VALID_LINKS.append(url)
            print_status(tag, "链接有效", "success")
            return True
        else:
            print_status(tag, f"HTTP {response.status_code}", "error")
            return False
    except requests.exceptions.RequestException as e:
        print_status(tag, f"网络错误: {type(e).__name__}", "warning")
        return False
    finally:
        time.sleep(0.05)

def main():
    # 命令行参数解析 (保持原有验证逻辑)
    parser = argparse.ArgumentParser(description='时空序列链接生成工具', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--base', type=str, required=True)
    parser.add_argument('--start_date', type=str, required=True)
    parser.add_argument('--end_date', type=str, required=True)
    parser.add_argument('--start_time', type=str, default="000000")
    parser.add_argument('--end_time', type=str, default="235959")
    parser.add_argument('--workers', type=int, default=50)
    
    try:
        args = parser.parse_args()
        
        # 验证日期参数
        start_date = validate_date(args.start_date)
        end_date = validate_date(args.end_date)
        validate_date_range(start_date, end_date)
        
        # 验证时间参数
        start_time = validate_time(args.start_time)
        end_time = validate_time(args.end_time)
        time_numbers = generate_time_numbers(start_time, end_time)
        
    except ValueError as e:
        print(f"\n\033[1;31m参数错误: {str(e)}\033[0m")
        sys.exit(1)
    
    # 生成所有任务
    tasks = []
    current_date = start_date
    while current_date <= end_date:
        filehead = f"{args.base}_{current_date.strftime('%Y%m%d')}"
        for tn in time_numbers:
            tasks.append( (filehead, tn) )
        current_date += timedelta(days=1)
    
    total_tasks = len(tasks)
    print(f"开始检查 {total_tasks} 个链接")
    
    # 执行检查
    success_count = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(check_link, fh, tn) for fh, tn in tasks]
        
        try:
            with tqdm(total=total_tasks, desc="检查进度",
                     unit="链接", disable=(os.getenv('CI') == 'true')) as pbar:
                for future in as_completed(futures):
                    if should_stop:
                        break
                    if future.result():
                        success_count += 1
                    pbar.update(1)
        except KeyboardInterrupt:
            pass
        
        if should_stop:
            for f in futures:
                f.cancel()
    
    # 保存结果
    with open(OUTPUT_FILE, 'w') as f:
        f.write("\n".join(VALID_LINKS))
    
    print(f"\n找到 {len(VALID_LINKS)} 个有效链接")
    print(f"结果已保存到: {OUTPUT_FILE}")

if __name__ == "__main__":
    if os.getenv('CI') != 'true':
        print("\033[2J\033[H")  # 本地运行时清屏
    main()
