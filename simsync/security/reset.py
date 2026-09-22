import argparse
import hashlib
import sys
from pathlib import Path

from simsync.config import load_config, save_config, get_config_path

# 解决 Windows 命令行下可能的编码问题
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def reset_credentials(
    new_password: str = "",
    clear_password: bool = False,
    disable_2fa: bool = False,
    port: int = 0,
    config_path: str = None,
    username: str = "",
):
    """
    重置管理员密码、用户名、端口与 2FA 的应急命令行工具
    可在 Docker 宿主机终端或容器内直接执行:
    docker exec -it simsync python -m simsync.security.reset
    """
    import time
    cfg = load_config(config_path)
    actual_path = config_path or get_config_path() or "config.yaml"

    modified = False

    if username:
        cfg.server.username = username.strip()
        modified = True
        print(f"[OK] 管理员用户名已更新为: {cfg.server.username}")

    if clear_password:
        cfg.server.username = "admin"
        cfg.server.password = ""
        cfg.server.password_hash = ""
        cfg.server.totp_enabled = False
        cfg.server.totp_secret = ""
        cfg.server.recovery_codes_hash = []
        cfg.server.secret_key = f"simsync-secret-key-{int(time.time())}"
        modified = True
        print("[OK] 管理员密码与 2FA 已彻底清空！下次打开网页将进入【首次初始化注册】向导设置新账号与密码。")

    elif new_password:
        new_hash = hashlib.sha256(new_password.encode("utf-8")).hexdigest().lower()
        cfg.server.password = ""
        cfg.server.password_hash = new_hash
        cfg.server.secret_key = f"simsync-secret-key-{int(time.time())}"
        modified = True
        print(f"[OK] 管理员密码已成功重置为新密码！用户名: {cfg.server.username or 'admin'} (已注销全部已登录会话)")

    if disable_2fa:
        cfg.server.totp_enabled = False
        cfg.server.totp_secret = ""
        cfg.server.recovery_codes_hash = []
        modified = True
        print("[OK] 2FA 双因子认证已成功停用！")

    if port and 1 <= port <= 65535:
        cfg.server.port = port
        modified = True
        print(f"[OK] Web 服务监听端口已更新为: {port}（重启容器或服务后生效）")

    if modified:
        if save_config(cfg, actual_path):
            print(f"[OK] 配置文件已更新: {actual_path}")
        else:
            print(f"[ERROR] 写入配置文件失败: {actual_path}")
    else:
        print("[INFO] 未指定任何操作，请使用 --help 查看选项。")


def main():
    parser = argparse.ArgumentParser(description="SimSync 应急安全凭据与端口管理工具")
    parser.add_argument("-u", "--username", type=str, help="设置管理员用户名 (默认: admin)")
    parser.add_argument("-p", "--password", type=str, help="设置新的管理员密码")
    parser.add_argument("-P", "--port", type=int, help="修改 Web 服务端口 (1-65535)")
    parser.add_argument("--clear", action="store_true", help="清空密码与 2FA，恢复首次初始化注册状态")
    parser.add_argument("--disable-2fa", action="store_true", help="仅关闭/停用 2FA 双因子认证")
    parser.add_argument("-c", "--config", type=str, help="指定配置文件路径")
    args = parser.parse_args()

    if not args.username and not args.password and not args.clear and not args.disable_2fa and not args.port:
        print("==========================================")
        print("   SimSync 应急安全凭据与端口管理")
        print("==========================================")
        print("1. 重置管理员密码")
        print("2. 修改 Web 访问端口")
        print("3. 仅关闭 2FA 双因子认证")
        print("4. 清空密码与 2FA (重新进入首次初始化向导)")
        print("5. 退出")
        try:
            choice = input("请选择操作 [1-5]: ").strip()
            if choice == "1":
                pwd = input("请输入新的管理员密码: ").strip()
                if pwd:
                    reset_credentials(new_password=pwd, config_path=args.config)
                else:
                    print("密码不能为空！")
            elif choice == "2":
                p_str = input("请输入新的 Web 端口 [1-65535]: ").strip()
                try:
                    p = int(p_str)
                    if 1 <= p <= 65535:
                        reset_credentials(port=p, config_path=args.config)
                    else:
                        print("端口号必须在 1 ~ 65535 之间！")
                except ValueError:
                    print("端口号必须为数字！")
            elif choice == "3":
                reset_credentials(disable_2fa=True, config_path=args.config)
            elif choice == "4":
                reset_credentials(clear_password=True, config_path=args.config)
            else:
                print("已退出。")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            sys.exit(0)
    else:
        reset_credentials(
            username=args.username or "",
            new_password=args.password or "",
            clear_password=args.clear,
            disable_2fa=args.disable_2fa,
            port=args.port or 0,
            config_path=args.config,
        )


if __name__ == "__main__":
    main()
