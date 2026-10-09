1.安装依赖‌：确保系统有编译工具。
sudo apt update
sudo apt install -y gcc make tcl-dev libjemalloc-dev

2.下载源码‌：从官网获取 8.0.0 压缩包。
wget http://download.redis.io/releases/redis-8.0.0.tar.gz
tar -xvf redis-8.0.0.tar.gz && cd redis-8.0.0

3.编译安装‌：执行编译并安装到系统目录。
make -j $(nproc)
sudo make install

4.启动服务‌：使用 systemd 管理 Redis 进程。
sudo systemctl start redis-server
sudo systemctl enable redis-server

5.检查状态‌：确认服务运行正常。
sudo systemctl status redis-server

6.配置防火墙‌：若需远程访问，放行 6379 端口。
sudo ufw allow 6379/tcp
