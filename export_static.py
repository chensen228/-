import os
import shutil
import urllib.request
import re

# 目标生成目录
DIST_DIR = os.path.join(os.path.dirname(__file__), "dist")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DIST_STATIC_DIR = os.path.join(DIST_DIR, "static")

# 要抓取的路由映射表 (URL路径 -> 输出文件名)
ROUTES = {
    "/": "index.html",
    "/library": "library.html",
    "/academic": "academic.html",
    "/practice": "practice.html",
    "/data-center": "data-center.html",
    "/governance-lab": "governance-lab.html",
    "/graph-lab": "graph-lab.html"
}

BASE_URL = "http://127.0.0.1:5050"

def clean_and_prepare():
    if os.path.exists(DIST_DIR):
        shutil.rmtree(DIST_DIR)
    os.makedirs(DIST_DIR, exist_ok=True)
    # 拷贝静态资源
    if os.path.exists(STATIC_DIR):
        shutil.copytree(STATIC_DIR, DIST_STATIC_DIR)
    print("已初始化 dist 目录并拷贝 static 资源。")

def rewrite_html(html_str):
    # 重写内部路由链接
    for route, filename in ROUTES.items():
        # 精确匹配 href="/xxx" 避免替换带参数的链接导致误伤，或者干脆简单粗暴点
        # 比如 href="/" 替换成 href="./index.html"
        html_str = re.sub(r'href=([\'"])' + route + r'([\'"])', r'href=\g<1>./' + filename + r'\g<2>', html_str)
        # 有些是带着参数跳转的，比如 href="/graph?student_id=xx"
        # 统一把带参数的路由直接砍掉参数，导向静态文件
        html_str = re.sub(r'href=([\'"])' + route + r'\?[^\'"]*([\'"])', r'href=\g<1>./' + filename + r'\g<2>', html_str)

    # 重写静态资源路径
    html_str = html_str.replace('href="/static/', 'href="./static/')
    html_str = html_str.replace('src="/static/', 'src="./static/')
    
    # 将所有的 form 提交拦截，弹出展示版免责声明
    # 找到所有的 <form action="/xxx"> 并把它改成不跳转且提示
    html_str = re.sub(
        r'<form[^>]*>', 
        r'<form onsubmit="alert(\'云端静态展示切片仅支持数据预览与图谱交互，无法进行真实的数据库写入操作。如需体验请部署本地完整版。\'); return false;">', 
        html_str
    )
    
    return html_str

def fetch_and_save():
    for route, filename in ROUTES.items():
        url = BASE_URL + route
        print(f"正在抓取 {url} ...")
        try:
            req = urllib.request.Request(url)
            # 伪造请求头，防止部分拦截
            req.add_header('User-Agent', 'Mozilla/5.0')
            with urllib.request.urlopen(req) as response:
                content = response.read().decode('utf-8')
                
                # 重写 HTML 内容
                rewritten_content = rewrite_html(content)
                
                # 写入文件
                filepath = os.path.join(DIST_DIR, filename)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(rewritten_content)
                print(f"  -> 保存为 {filename} 成功。")
        except Exception as e:
            print(f"抓取 {url} 失败: {e}")

if __name__ == "__main__":
    print("=== 开始执行智慧校园系统静态化劫持 ===")
    clean_and_prepare()
    fetch_and_save()
    print("=== 所有页面静态化导出完成，存放在 dist 目录下 ===")
