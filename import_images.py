import tkinter as tk
from tkinter import filedialog
import shutil
import os

def main():
    # 隐藏主窗口
    root = tk.Tk()
    root.withdraw()
    
    # 弹出提示，确保窗口在最前面
    root.attributes('-topmost', True)

    print("正在等待用户选择图片...")
    
    # 弹出文件选择框
    file_paths = filedialog.askopenfilenames(
        title="请选中那 4 张你学校的照片（按住 Ctrl 可以多选）",
        filetypes=[("Image files", "*.jpg *.jpeg *.png")]
    )
    
    if not file_paths:
        print("没有选择图片，操作取消。")
        return
        
    target_dir = os.path.join(os.path.dirname(__file__), "static")
    os.makedirs(target_dir, exist_ok=True)
    
    count = 1
    for path in file_paths[:4]: # 最多处理 4 张
        # 强制另存为 bg{i}.jpg
        target_path = os.path.join(target_dir, f"bg{count}.jpg")
        try:
            shutil.copy2(path, target_path)
            print(f"成功导入: bg{count}.jpg")
            count += 1
        except Exception as e:
            print(f"导入 {path} 失败: {e}")
            
    print("全部导入完成！现在你可以去刷新网页了！")

if __name__ == "__main__":
    main()
