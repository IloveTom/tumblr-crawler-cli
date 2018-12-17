# coding:utf-8
"""
Created by tzw0745 at 18-9-28
"""
# region import
import json
import os
import re
import shutil
import time
from datetime import datetime
from threading import Thread

import requests
from lxml import etree, html

try:
    # Python3 import
    from urllib.parse import urlsplit
    from queue import Queue, Empty
except ImportError:
    # Python2 import
    from urlparse import urlparse as urlsplit
    from Queue import Queue, Empty

try:
    # after Python3.2
    from tempfile import TemporaryDirectory

    temp_dir = TemporaryDirectory('tumblr_crawler_cli')
except (ImportError, ImportError):
    temp_dir = '.tumblr_crawler_cli'
    os.mkdir(temp_dir) if not os.path.exists(temp_dir) else None

from args import parser
from utils import safe_format, clean_fn

# endregion

queue_sites = Queue()  # 待解析站点队列
queue_down = Queue()  # 下载任务队列
down_stop = False  # 下载停止信号
cli_args = parser.parse_args()  # 命令行参数

# 默认全部下载
if not cli_args.down_photo and not cli_args.down_video:
    cli_args.down_photo = cli_args.down_video = True
# 创建http request session并设置代理
session = requests.session()
if cli_args.proxy:
    session.proxies = {'http': cli_args.proxy, 'https': cli_args.proxy}
# 初始化待解析站点队列
for _site in cli_args.sites:
    queue_sites.put(_site)

# 当post信息非标准格式时解析图片的正则
photo_regex = re.compile(r'https://\d+.media.tumblr.com/\w{32}/tumblr_[\w.]+')


def _get(url, params=None, **kwargs):
    """
    向目标链接发送一个http请求，requests.get包裹方法
    :param url:
    :param params:
    :param kwargs:
    :return: requests.Response
    """
    global cli_args, session
    for _retry in range(cli_args.retries):
        time.sleep(cli_args.interval)
        try:
            r = session.get(url, params=params, **kwargs)
            if r.status_code in (200, 404):
                break
        except requests.exceptions.RequestException:
            pass
    else:
        time.sleep(cli_args.interval)
        r = session.get(url, params=params, **kwargs)

    return r


def parse_site_thread():
    """
    添加图片、视频下载任务到下载任务队列
    """
    global queue_sites, cli_args, session
    while not queue_sites.empty():
        try:
            site_name = queue_sites.get(block=True, timeout=0.5)
        except Empty:
            break

        print('start crawler tumblr site: {}'.format(site_name))
        site_dir = os.path.join(cli_args.save_dir, site_name)
        os.mkdir(site_dir) if not os.path.exists(site_dir) else None

        global queue_down
        if cli_args.down_photo:
            for post in tumblr_posts(site_name, 'photo', get_method=_get):
                # 将图片url加入下载队列
                for photo_url in post['photos']:
                    uid_reg = r'[/_]([a-zA-Z0-9]{8,})_'
                    uid = re.findall(uid_reg, photo_url)[0]
                    args = {'post_id': post['id'], 'type': post['type'],
                            'uid': uid, 'date': post['gmt'],
                            'timestamp': post['timestamp']}
                    ext = re.findall(r'\.[a-zA-Z0-9]{3,}$', photo_url)[0]
                    filename = safe_format(cli_args.fn_fmt, **args) + ext
                    file_path = os.path.join(site_dir, clean_fn(filename))
                    queue_down.put((file_path, photo_url))
        if cli_args.down_video:
            for post in tumblr_posts(site_name, 'video', get_method=_get):
                # 将视频url加入下载队列
                uid = re.findall(r'tumblr_([a-zA-Z0-9]{15,})', post['video'])[0]
                args = {'post_id': post['id'], 'type': post['type'],
                        'uid': uid, 'date': post['gmt'],
                        'timestamp': post['timestamp']}
                filename = safe_format(cli_args.fn_fmt, **args)
                ext = '.' + post['ext']
                file_path = os.path.join(site_dir, clean_fn(filename + ext))
                queue_down.put((file_path, post['video']))


def download_thread(thread_name):
    """
    持续从下载任务队列获取任务并下载文件，直到stop_sing为True
    :param thread_name: 线程名称，用于输出
    :return:
    """
    msg = ' '.join(['Thread', str(thread_name), '{}: {}'])
    global queue_down, down_stop, temp_dir, cli_args
    while not down_stop:
        # 从下载任务队列中获取一个任务
        if queue_down.empty():
            continue
        try:
            task_path, task_url = queue_down.get(block=True, timeout=0.5)
        except Empty:
            continue
        # 判断文件是否存在
        if not cli_args.overwrite and os.path.isfile(task_path):
            print(msg.format('Exists', task_path))
            continue
        # 向url发送请求
        try:
            r = _get(task_url, timeout=3)
        except requests.exceptions.RequestException as e:
            # 请求失败
            print(msg.format('RequestException', task_path))
            print(str(e))
            continue
        # 先写入临时文件
        _temp_name = 'tumblr_thread_{}.downloading'.format(thread_name)
        _temp_path = os.path.join(
            temp_dir if isinstance(temp_dir, str) else temp_dir.name,
            _temp_name
        )
        chunk_size = 2 * 1024 * 1024  # 2M缓存
        try:
            with open(_temp_path, 'wb') as f:
                for content in r.iter_content(chunk_size=chunk_size):
                    f.write(content)
            # 判断文件大小是否符合参数设置
            if cli_args.min_size and \
                    os.path.getsize(_temp_path) < cli_args.min_size:
                print(msg.format('Too Small', task_path))
                continue
            # 下载完后再移动到目标目录
            shutil.move(_temp_path, task_path)
        except (IOError, OSError):
            print(msg.format('IO/OSError', _temp_path))
            print(msg.format('IO/OSError', task_path))
            continue
        print(msg.format('Completed', task_path))


def tumblr_posts(site, post_type, get_method=requests.get):
    """
    获取tumblr站点下所有的图片或视频信息
    :param site: 站点名称
    :param post_type: 文章类型，包括photo和video
    :param get_method: 发送GET请求使用的方法
    :return: 图片或视频信息列表迭代器
    """
    if not re.match(r'^[a-zA-Z0-9_-]+$', site):
        raise ValueError('Param "site" not match "^[a-zA-Z0-9_-]+$"')
    if post_type not in ('photo', 'video'):
        raise ValueError('Param "post_type" must be "photo" or "video"')

    def _max_width_sub(node, sub_name):
        """
        获取node下max-width属性最大的子节点的文本
        :param node: xml父节点
        :param sub_name: 子节点名称
        :return: 子节点的文本
        """
        return sorted(
            node.findall(sub_name),
            key=lambda _i: int(_i.get('max-width', '0'))
        )[-1].text

    page_size, start = 50, 0
    gmt_fmt = '%Y-%m-%d %H:%M:%S GMT'
    while True:
        api = 'http://{}.tumblr.com/api/read'.format(site)
        params = {'type': post_type, 'num': page_size, 'start': start}
        start += page_size
        # 获取文章列表
        r = get_method(api, params=params, timeout=3)
        if r.status_code == 404:
            raise ValueError('tumblr site "{}" not found'.format(site))
        posts = etree.fromstring(r.content).find('posts').findall('post')
        if not posts:
            break

        for post in posts:
            post_info = {
                'id': post.get('id'),
                'gmt': datetime.strptime(post.get('date-gmt'), gmt_fmt),
                'type': post_type,
                'timestamp': post.get('unix-timestamp')
            }
            if post_type == 'photo':
                # 获取文章下所有图片链接
                if post.findall('photo-url'):  # 标准格式
                    photos = []
                    for photo_set in post.iterfind('photoset'):
                        for photo in photo_set.iterfind('photo'):
                            photos.append(_max_width_sub(photo, 'photo-url'))
                    first_photo = _max_width_sub(post, 'photo-url')
                    if first_photo not in photos:
                        photos.append(first_photo)
                else:  # 非标准格式，用正则
                    photos = photo_regex.findall(''.join(post.itertext()))
                post_info['photos'] = list(set(photos))
                yield post_info
            elif post_type == 'video':
                # 获取视频链接
                try:
                    video_ext = post.find('video-source').find('extension').text
                except AttributeError:  # 忽略非标准格式
                    continue
                tree = html.fromstring(_max_width_sub(post, 'video-player'))
                options = json.loads(tree.get('data-crt-options'))
                if not options['hdUrl']:
                    options['hdUrl'] = tree.getchildren()[0].get('src')
                post_info.update({'video': options['hdUrl'], 'ext': video_ext})
                yield post_info


def main():
    global queue_down, down_stop, temp_dir
    # 多线程解析站点（最多3个线程）
    parse_thread_pool = []
    for i in range(min(len(cli_args.sites), 3)):
        _t = Thread(target=parse_site_thread)
        _t.setDaemon(True)
        _t.start()
        parse_thread_pool.append(_t)

    # 多线程下载
    down_thread_pool = []
    for i in range(cli_args.thread_num):
        _t = Thread(target=download_thread, args=(i,))
        _t.setDaemon(True)
        _t.start()
        down_thread_pool.append(_t)

    # 等待站点解析线程结束
    for thread in parse_thread_pool:
        thread.join()
    # 等待下载任务队列清空
    while not queue_down.empty():
        time.sleep(0.5)
        continue
    # 发送下载停止信号并等待下载线程结束
    down_stop = True
    for thread in down_thread_pool:
        thread.join()

    # 移除临时文件夹
    if isinstance(temp_dir, str) and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    main()
