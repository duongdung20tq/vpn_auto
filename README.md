# VPN Auto: Pulse Secure / Cisco Secure Client + token FPT-CA

Ứng dụng Windows giữ kết nối VPN luôn bật, tự nhập PIN token FPT-CA, hỗ trợ hai loại client:

- **Cisco Secure Client** (gateway `btcvpn.mof.gov.vn`, nhóm `Cert-kyso`).
- **Pulse Secure** (server `vpn.mof.gov.vn`).

Việc tool làm:

- Có mạng là tự kết nối; mất kết nối thì tự nối lại; mất mạng thì chờ rồi nối ngay khi có mạng.
- Hộp thoại **Xác nhận PIN** của FPT-CA hiện ra lúc nào thì tự nhập PIN (kể cả khi token hỏi lại sau một thời gian).
- Với Cisco: tự chọn nhóm `Cert-kyso`, điền tài khoản, mật khẩu, và chấp nhận banner.
- Với Pulse: tự điền tài khoản, mật khẩu, tích **Save settings**.
- PIN hoặc mật khẩu bị từ chối thì **dừng tự điền** để không khóa token/tài khoản.
- Trước mỗi lần kết nối: **đăng xuất mọi phiên Windows khác**, trừ Administrator và tài khoản đang chạy tool (`logoff_others = 1`; thay cho `logout_all.bat` trước đây). Sau đó **tắt Remote Desktop** (`block_rdp = 1`) và **bật lại khi VPN đã kết nối** (hoặc khi tạm dừng, bị khóa, thoát tool). Cần chạy tool bằng quyền Administrator; nếu RDP vốn đã tắt thì tool không bật lên.
- Với Cisco, nếu **chưa cắm token** (không thấy chứng thư FPT-CA) thì báo rõ thay vì thử kết nối vô ích.

Đã chạy thật end-to-end với Cisco Secure Client 5.1.17 và FPT-CA Token Manager v4 trên Windows 11 (xem *Kết quả test*).

## Yêu cầu trên máy chạy tool

- Windows 10/11. `VpnAuto.exe` chạy độc lập, không cần cài Python.
- **Cisco**: đã cài Cisco Secure Client; token FPT-CA đang cắm và FPT-CA Token Manager đang chạy (để đẩy chứng thư vào kho Windows).
- **Pulse**: đã cài Pulse Secure với một kết nối tới server; token FPT-CA đang cắm.

## Triển khai nhiều máy bằng file cấu hình

Giải nén `VpnAuto-release.zip` ra một thư mục, ví dụ `C:\VpnAuto\`. Gói gồm `VpnAuto.exe` (VPN), `RdpAuto.exe` (tự kết nối lại Remote Desktop), `vpn_auto.example.ini`, `README.md`.

1. Đổi tên `vpn_auto.example.ini` thành `vpn_auto.ini`, điền `password` và `pin`:

   ```ini
   [vpn]
   backend = cisco
   server = btcvpn.mof.gov.vn
   group = Cert-kyso
   username = caotienhai-fpt
   password = <mật khẩu VPN>
   pin = <PIN token>
   auto_connect = 1
   ```

2. Đặt `vpn_auto.ini` cạnh `VpnAuto.exe`, chạy `VpnAuto.exe`.
3. Lần chạy đầu, tool nhập cấu hình vào máy (mã hóa DPAPI) và **tự xóa `password` và `pin` khỏi file**.

Đổi file ini sau này thì tool nhập lại khi khởi động; không đổi thì tool giữ nguyên cấu hình bạn chỉnh trên giao diện.

> Cấu hình mã hóa theo máy và theo tài khoản Windows, nên **không copy `config.json` sang máy khác**. Mỗi máy nhập một lần, bằng file ini hoặc trên giao diện.

Không dùng file ini thì mở `VpnAuto.exe`, chọn **Loại VPN**, điền phần **Cấu hình**, bấm **Lưu cấu hình**.

## Giao diện

- **Trạng thái**: đèn màu, kết nối → server, chi tiết. Nút **Kết nối ngay**, **Ngắt kết nối**, **Bỏ khóa**, ô **Tự động kết nối khi có mạng**.
- **Cấu hình**: chọn Loại VPN (Cisco/Pulse). Cisco hiện ô Gateway + Nhóm; Pulse hiện ô Kết nối Pulse + Save settings. Chung: Tài khoản, Mật khẩu, PIN. Nút **Lưu cấu hình**, **Nhập từ file .ini**, **Xuất file mẫu .ini**.
- **Nhật ký**: log trực tiếp; nút **Mở ứng dụng VPN**, **Mở thư mục dữ liệu**.

Màu: xanh lá = đã kết nối, vàng = đang kết nối, đỏ = mất kết nối / chờ mạng / chưa thấy token, xám = tắt tự động, tím = dừng do PIN/mật khẩu sai (sửa rồi bấm **Bỏ khóa**).

Đóng cửa sổ = thu xuống khay, tool vẫn chạy. Chạy `VpnAuto.exe` lần nữa thì mở lại cửa sổ. **Thoát hẳn** để dừng.

Dòng lệnh: `VpnAuto.exe run --minimized` (thu nhỏ dưới khay), `VpnAuto.exe run --paused` (không tự kết nối lần này), `VpnAuto.exe status`, `VpnAuto.exe watch`.

## Cách hoạt động (Cisco)

Tool dùng công cụ dòng lệnh chính thức `vpncli.exe` để kết nối/ngắt/đọc trạng thái, và điều khiển hộp thoại PIN của FPT-CA qua Win32. Vì giao diện `csc_ui.exe` giữ quyền kết nối, tool đóng nó trước khi kết nối bằng CLI; đường hầm do dịch vụ `vpnagent` duy trì.

Trình tự một lần kết nối (đã quan sát thật):

1. `connect btcvpn.mof.gov.vn`
2. Hộp thoại **Xác nhận PIN** (FPT-CA) → tool nhập PIN, bấm Đăng nhập.
3. Posture Assessment + Hostscan (khoảng 30–45 giây).
4. Chọn nhóm → `Cert-kyso`.
5. Username → tài khoản, Password → mật khẩu.
6. Banner "This is the BTC'VPN System" → chấp nhận.
7. **Connected**.

Nếu không thấy chứng thư token (token chưa cắm hoặc FPT-CA Token Manager chưa chạy), Cisco báo "no client certificate"; tool hiển thị "Chưa thấy token - kiểm tra token đã cắm chưa" và chờ, không thử kết nối vô ích.

## Dữ liệu và bảo mật

- Cấu hình: `%APPDATA%\vpn_auto\config.json`. PIN và mật khẩu mã hóa bằng Windows DPAPI.
- Log: `%APPDATA%\vpn_auto\vpn_auto.log`, không ghi PIN/mật khẩu.
- Tool chỉ nhập PIN vào hộp thoại thuộc tiến trình VPN client, không đụng hộp thoại PIN của trình duyệt hay phần mềm ký số.
- Dừng tự điền khi token báo "Mã PIN sai / đã bị khóa", hoặc khi bị hỏi lại tài khoản ngay sau khi vừa điền.

## Kết quả test (2026-10-09)

| Tình huống | Kết quả |
|---|---|
| Cisco: tự kết nối (PIN → Posture → group → tài khoản → banner) | **Connected** sau ~50 giây, rồi ngắt |
| Cisco: watchdog tự nhập PIN qua hộp thoại "Xác nhận PIN" | Nhập đúng, Đăng nhập thành công |
| Cisco: token rút ra | Báo "Chưa thấy token", không thử vô ích (đã kiểm chứng) |
| Pulse: tự kết nối / nối lại / mở lại Pulse khi bị tắt | Đã kiểm chứng ở các lần test trước |

Chưa test: mất mạng thật khi đang kết nối Cisco, và việc token tự hỏi lại PIN sau nhiều giờ với Cisco.

## Tool 2: RDP Auto — tự kết nối lại Remote Desktop

Sau khi bật VPN, kết nối Remote Desktop tới máy đích hay rớt gói ở lần bắt tay đầu (dù ping thông), phải bấm Connect nhiều lần mới vào. `RdpAuto.exe` tự thử lại giúp.

Dùng: mở `RdpAuto.exe`, nhập **IP**, (tùy chọn) **Tài khoản** và **Mật khẩu**, bấm **Kết nối**.

- Có mật khẩu → tool lưu vào Windows Credential Manager (đúng như "remember me" của mstsc) và mstsc tự đăng nhập, vào thẳng.
- Để trống mật khẩu → tool thử lại tới khi vào được màn hình, rồi bạn tự nhập.
- Tích **Nhớ máy này** để lần sau chỉ chọn IP là xong (mật khẩu mã hóa DPAPI). **Xóa máy đã lưu** để gỡ.

Cách hoạt động: tool probe cổng 3389; khi tới được thì mở `mstsc /v:<ip>` và chờ đến khi có kết nối TCP **ESTABLISHED** tới `<ip>:3389` (tín hiệu đã vào được). Nếu lần đó thất bại (rớt bắt tay, hiện hộp lỗi, hoặc mstsc tự đóng) thì đóng cửa sổ đó và thử lại, lặp tới khi vào được. Chỉ đóng đúng cửa sổ mstsc do tool mở, không đụng phiên RDP khác của bạn.

Dòng lệnh: `RdpAuto.exe` (mở giao diện) · `VpnAuto.exe rdp <ip>` (mở giao diện) · `VpnAuto.exe rdp --console <ip> --username <u>` (chạy trong console, đọc `RDP_PASSWORD` từ biến môi trường với `--from-env`).

Đã test thật: kết nối `10.15.68.218` tự điền mật khẩu, vào trong ~1 giây; với IP chưa tới được thì báo "đang chờ" và thử lại, không mở mstsc vô ích.

## Phát triển

```powershell
python -m pip install -r requirements.txt
python -m pytest -q tests      # unit test, dùng engine giả, không đụng VPN thật
.\build.ps1                    # test + build dist\VpnAuto.exe + dist\VpnAuto-release.zip
python -m vpn_auto status      # trạng thái và cấu hình
```

| File | Vai trò |
|---|---|
| `vpn_auto/winui.py` | Hàm Win32 |
| `vpn_auto/cisco.py`, `cisco_watchdog.py` | Điều khiển Cisco + vòng giám sát |
| `vpn_auto/pulse.py`, `watchdog.py` | Điều khiển Pulse + vòng giám sát |
| `vpn_auto/network.py` | Kiểm tra tới được server |
| `vpn_auto/config.py`, `dpapi.py` | Cấu hình mã hóa |
| `vpn_auto/deploy.py` | Nhập/xuất `vpn_auto.ini` |
| `vpn_auto/app.py`, `ui.py`, `tray.py` | Controller, cửa sổ, khay |
| `vpn_auto/cli.py` | Lệnh dòng lệnh |
