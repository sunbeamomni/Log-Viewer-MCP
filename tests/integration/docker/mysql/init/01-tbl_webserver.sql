-- 测试专用的管理库：tbl_webserver 与测试数据（只用于集成测试）。
-- 账号 logview 由 MYSQL_USER 创建，对 admin 库有全部权限：与线上"沿用现有账号"一致，只读由程序保证（需求 7），不靠账号权限。
SET NAMES utf8mb4;
USE admin;

CREATE TABLE tbl_webserver (
  svr_id   INT UNSIGNED NOT NULL PRIMARY KEY,
  svr_name VARCHAR(64)  NOT NULL,
  zone     VARCHAR(32)  NOT NULL,
  ip_inner VARCHAR(64)  NULL,
  is_test  TINYINT      NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 172.30.77.11 / .12 / .13 是 compose 中 gs、gs-noionice、gs-grepspy 的固定地址；.99 没有主机（SSH_FAILED）。
-- 1005–1013 的 ip_inner 不是合法的点分十进制 IPv4（INVALID_SERVER_ADDRESS，7.5）。
INSERT INTO tbl_webserver (svr_id, svr_name, zone, ip_inner, is_test) VALUES
  (1001,       'S1-测试',         'eu',   '172.30.77.11', 0),
  (1002,       'S2-无ionice',     'eu',   '172.30.77.12', 0),
  (1003,       'S3-grep记录',     'eu',   '172.30.77.13', 0),
  (1004,       'S4-不可达',       'eu',   '172.30.77.99', 0),
  (1005,       '坏地址-越界',     'eu',   '10.0.1.256', 0),
  (1006,       '坏地址-前导零',   'eu',   '172.030.77.11', 0),
  (1007,       '坏地址-空串',     'eu',   '', 0),
  (1008,       '坏地址-注入',     'eu',   '172.30.77.11; rm -rf /', 0),
  (1009,       '坏地址-末尾换行', 'eu',   '172.30.77.11\n', 0),
  (1010,       '坏地址-主机名',   'eu',   'gs', 0),
  (1011,       '坏地址-IPv6',     'eu',   '::1', 0),
  (1012,       '坏地址-NULL',     'eu',   NULL, 0),
  (1013,       '坏地址-非ASCII数字', 'eu', '١٧٢.30.77.11', 0),
  (2001,       'T1-测试服',       'test', '172.30.77.11', 1),
  (4294967295, 'S-max',           'eu',   '172.30.77.11', 0);
