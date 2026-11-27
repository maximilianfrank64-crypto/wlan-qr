"""WLAN-QR-Tool: aktuelles WLAN als QR-Code zeigen und per Webcam einem WLAN beitreten."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter.font as tkfont
from tkinter import filedialog
from xml.sax.saxutils import escape as xml_escape
import xml.etree.ElementTree as ET

import customtkinter as ctk
import cv2
import qrcode
from PIL import Image

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def run(args):
    r = subprocess.run(args, capture_output=True, encoding="oem", errors="replace",
                       creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------- WLAN lesen ----------

def current_wifi():
    """Gibt (ssid, profilname) des verbundenen WLANs zurück oder (None, None)."""
    _, out = run(["netsh", "wlan", "show", "interfaces"])
    ssid = profile = None
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if key == "SSID" and ssid is None:
            ssid = val
        elif key in ("Profil", "Profile") and profile is None:
            profile = val
    if not ssid:
        return None, None
    return ssid, profile or ssid


def wifi_details(profile):
    """Liest Typ und Passwort über den Profil-Export (sprachunabhängig)."""
    folder = tempfile.mkdtemp(prefix="wlanqr_")
    try:
        run(["netsh", "wlan", "export", "profile", f"name={profile}", "key=clear", f"folder={folder}"])
        files = [f for f in os.listdir(folder) if f.lower().endswith(".xml")]
        if not files:
            return None
        ns = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}
        root = ET.parse(os.path.join(folder, files[0])).getroot()
        auth = root.findtext(".//w:authentication", default="open", namespaces=ns)
        key = root.findtext(".//w:keyMaterial", default="", namespaces=ns)
        hidden = root.findtext(".//w:nonBroadcast", default="false", namespaces=ns) == "true"
        if auth.lower() == "open" and not key:
            typ = "nopass"
        elif auth.lower() == "open":
            typ = "WEP"
        else:
            typ = "WPA"
        return {"type": typ, "password": key, "hidden": hidden}
    finally:
        for f in os.listdir(folder):
            os.remove(os.path.join(folder, f))
        os.rmdir(folder)


def qr_escape(s):
    for ch in ["\\", ";", ",", ":", '"']:
        s = s.replace(ch, "\\" + ch)
    return s


def wifi_qr_text(ssid, typ, password, hidden):
    text = f"WIFI:T:{typ};S:{qr_escape(ssid)};"
    if typ != "nopass":
        text += f"P:{qr_escape(password)};"
    if hidden:
        text += "H:true;"
    return text + ";"


# ---------- WLAN-QR lesen und beitreten ----------

def parse_wifi_qr(text):
    if not text.upper().startswith("WIFI:"):
        return None
    body = text[5:]
    fields, key, buf, i = {}, None, "", 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            buf += body[i + 1]
            i += 2
            continue
        if c == ":" and key is None:
            key, buf = buf.upper(), ""
        elif c == ";":
            if key:
                fields[key] = buf
            key, buf = None, ""
        else:
            buf += c
        i += 1
    if key:
        fields[key] = buf
    if not fields.get("S"):
        return None
    return {
        "ssid": fields["S"],
        "type": (fields.get("T") or "nopass").upper(),
        "password": fields.get("P", ""),
        "hidden": fields.get("H", "").lower() == "true",
    }


def profile_xml(ssid, auth, enc, password, hidden):
    ssid_hex = ssid.encode("utf-8").hex().upper()
    sec = ""
    if auth != "open" or enc != "none":
        key_type = "networkKey" if enc == "WEP" else "passPhrase"
        sec = (f"<sharedKey><keyType>{key_type}</keyType><protected>false</protected>"
               f"<keyMaterial>{xml_escape(password)}</keyMaterial></sharedKey>")
    return f"""<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{xml_escape(ssid)}</name>
  <SSIDConfig><SSID><hex>{ssid_hex}</hex><name>{xml_escape(ssid)}</name></SSID>
  <nonBroadcast>{"true" if hidden else "false"}</nonBroadcast></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>auto</connectionMode>
  <MSM><security>
    <authEncryption><authentication>{auth}</authentication><encryption>{enc}</encryption><useOneX>false</useOneX></authEncryption>
    {sec}
  </security></MSM>
</WLANProfile>"""


def connect_wifi(info, status_cb):
    ssid = info["ssid"]
    if info["type"] == "NOPASS":
        variants = [("open", "none")]
    elif info["type"] == "WEP":
        variants = [("open", "WEP")]
    else:
        variants = [("WPA2PSK", "AES"), ("WPA3SAE", "AES"), ("WPAPSK", "TKIP")]

    for auth, enc in variants:
        status_cb(f"Verbinde mit {ssid} ({auth}) ...")
        fd, path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(profile_xml(ssid, auth, enc, info["password"], info["hidden"]))
            code, out = run(["netsh", "wlan", "add", "profile", f"filename={path}", "user=current"])
        finally:
            os.remove(path)
        if code != 0:
            continue
        run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"])
        for _ in range(20):
            time.sleep(1)
            now, _ = current_wifi()
            if now == ssid:
                return True
    return False


# ---------- Oberfläche ----------

ACCENT = ("#0A66D8", "#3B8CFF")
OK_COLOR = ("#1E8E3E", "#4CC774")
ERR_COLOR = ("#C5221F", "#FF6B62")
MUTED = ("#5F6368", "#A0A4AB")
CARD = ("#FFFFFF", "#1F2023")
BG = ("#F2F3F5", "#141517")
DEMO = "--demo" in sys.argv


class App(ctk.CTk):
    BASE_W, BASE_H = 480, 640

    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("WLAN-QR")
        self.geometry(f"{self.BASE_W}x{self.BASE_H}")
        self.minsize(240, 320)
        here = os.path.dirname(os.path.abspath(__file__))
        icon = os.path.join(here, "wlan_qr.ico")
        if os.path.exists(icon):
            self.after(250, lambda: self.iconbitmap(icon))

        self.cam = None
        self.scanning = False
        self.qr_pil = None
        self.cam_pil = None
        self.password = ""
        self.detector = cv2.QRCodeDetector()
        self.resize_job = None

        family = "Segoe UI Variable Display" if "Segoe UI Variable Display" in tkfont.families() else "Segoe UI"
        self.f_title = ctk.CTkFont(family=family, size=22, weight="bold")
        self.f_body = ctk.CTkFont(family=family, size=14)
        self.f_small = ctk.CTkFont(family=family, size=12)
        self.f_mono = ctk.CTkFont(family="Consolas", size=15)

        # Umschalter: zeigt immer, wo man ist
        self.mode = ctk.CTkSegmentedButton(
            self, values=["Teilen", "Beitreten"], command=self.on_mode, font=self.f_body,
            height=36, corner_radius=10, selected_color=ACCENT, selected_hover_color=ACCENT)
        self.mode.pack(side="top", fill="x", padx=16, pady=(16, 8))

        # Karte
        self.card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=18)
        self.card.pack(side="top", fill="both", expand=True, padx=16, pady=(4, 16))

        self.title_lbl = ctk.CTkLabel(self.card, text="", font=self.f_title, anchor="center")
        self.title_lbl.pack(side="top", fill="x", padx=16, pady=(18, 0))
        self.sub_lbl = ctk.CTkLabel(self.card, text="", font=self.f_small, text_color=MUTED, anchor="center")
        self.sub_lbl.pack(side="top", fill="x", padx=16, pady=(2, 8))

        self.actions = ctk.CTkFrame(self.card, fg_color="transparent")
        self.actions.pack(side="bottom", fill="x", padx=16, pady=(8, 16))

        self.area = ctk.CTkFrame(self.card, fg_color="transparent")
        self.area.pack(side="top", fill="both", expand=True, padx=16, pady=4)
        self.area.pack_propagate(False)
        self.view_img = ctk.CTkImage(light_image=Image.new("RGB", (1, 1), "white"), size=(1, 1))
        self.view = ctk.CTkLabel(self.area, text="", image=self.view_img, corner_radius=12)
        self.view.pack(expand=True)

        self.bind("<Configure>", self.on_configure)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.mode.set("Teilen")
        self.show_share()

    # --- Skalierung ---
    def scaling(self):
        return ctk.ScalingTracker.get_window_scaling(self)

    def on_configure(self, event):
        if event.widget is not self:
            return
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(50, self.apply_scale)

    def apply_scale(self):
        self.resize_job = None
        k = self.scaling()
        w, h = self.winfo_width() / k, self.winfo_height() / k
        s = max(0.6, min(1.5, min(w / self.BASE_W, h / self.BASE_H)))
        self.f_title.configure(size=max(11, round(22 * s)))
        self.f_body.configure(size=max(9, round(14 * s)))
        self.f_small.configure(size=max(8, round(12 * s)))
        self.f_mono.configure(size=max(9, round(15 * s)))
        wrap = max(120, int(w - 64))
        self.title_lbl.configure(wraplength=wrap)
        self.sub_lbl.configure(wraplength=wrap)
        self.refresh_image()

    def area_size(self):
        k = self.scaling()
        return (max(40, int(self.area.winfo_width() / k) - 4),
                max(40, int(self.area.winfo_height() / k) - 4))

    def refresh_image(self):
        aw, ah = self.area_size()
        if self.scanning and self.cam_pil is not None:
            iw, ih = self.cam_pil.size
            f = min(aw / iw, ah / ih)
            self.view_img.configure(light_image=self.cam_pil, size=(int(iw * f), int(ih * f)))
        elif self.qr_pil is not None:
            side = min(aw, ah)
            self.view_img.configure(light_image=self.qr_pil, size=(side, side))
        else:
            self.view_img.configure(light_image=Image.new("RGB", (1, 1)), size=(1, 1))
            self.view.configure(fg_color="transparent")
            return
        self.view.configure(fg_color="white" if not self.scanning else "transparent")

    # --- Hilfen ---
    def clear_actions(self):
        for w in self.actions.winfo_children():
            w.destroy()

    def set_text(self, title, sub="", sub_color=MUTED):
        self.title_lbl.configure(text=title)
        self.sub_lbl.configure(text=sub, text_color=sub_color)

    def button(self, text, cmd, primary=False):
        if primary:
            return ctk.CTkButton(self.actions, text=text, command=cmd, font=self.f_body, height=38,
                                 corner_radius=10, fg_color=ACCENT)
        return ctk.CTkButton(self.actions, text=text, command=cmd, font=self.f_body, height=38,
                             corner_radius=10, fg_color="transparent", border_width=1,
                             border_color=("#D0D3D8", "#3A3C40"), text_color=("#1F1F1F", "#E8E8E8"),
                             hover_color=("#EEF0F3", "#2A2C30"))

    def on_mode(self, value):
        if value == "Teilen":
            self.show_share()
        else:
            self.start_scan()

    # --- Teilen ---
    def show_share(self):
        self.stop_scan()
        self.clear_actions()
        self.mode.set("Teilen")
        if DEMO:
            ssid, d = "Beispiel-WLAN", {"type": "WPA", "password": "beispiel-passwort", "hidden": False}
        else:
            ssid, profile = current_wifi()
            if not ssid:
                self.qr_pil = None
                self.set_text("Kein WLAN", "Der PC ist gerade mit keinem WLAN verbunden.")
                self.button("Neu laden", self.show_share, primary=True).pack(fill="x")
                self.refresh_image()
                return
            d = wifi_details(profile)
            if d is None:
                self.qr_pil = None
                self.set_text(ssid, "Das Passwort konnte nicht gelesen werden.", ERR_COLOR)
                self.refresh_image()
                return
        self.password = d["password"]
        text = wifi_qr_text(ssid, d["type"], d["password"], d["hidden"])
        self.qr_pil = qrcode.make(text, box_size=20, border=2).get_image().convert("RGB")
        self.set_text(ssid, "Mit der Handy-Kamera scannen, um beizutreten.")
        self.refresh_image()

        self.actions.columnconfigure((0, 1), weight=1, uniform="a")
        self.pw_lbl = ctk.CTkLabel(self.actions, text="", font=self.f_mono)
        self.pw_btn = self.button("Passwort zeigen", self.toggle_pw)
        self.pw_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.button("Als Bild speichern", self.save_qr).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def toggle_pw(self):
        if self.pw_lbl.winfo_ismapped():
            self.pw_lbl.grid_forget()
            self.pw_btn.configure(text="Passwort zeigen")
        else:
            self.pw_lbl.configure(text=self.password or "(kein Passwort)")
            self.pw_lbl.grid(row=1, column=0, columnspan=2, pady=(10, 0))
            self.pw_btn.configure(text="Passwort verstecken")

    def save_qr(self):
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG-Bild", "*.png")],
                                            initialfile="WLAN-QR.png")
        if path:
            self.qr_pil.save(path)
            self.sub_lbl.configure(text="Gespeichert.", text_color=OK_COLOR)

    # --- Beitreten ---
    def start_scan(self):
        self.stop_scan()
        self.clear_actions()
        self.mode.set("Beitreten")
        self.qr_pil = None
        self.set_text("Kamera startet …")
        self.refresh_image()
        self.update_idletasks()
        self.cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cam.isOpened():
            self.cam = None
            self.set_text("Keine Kamera", "Keine Webcam gefunden, oder ein anderes Programm benutzt sie.", ERR_COLOR)
            self.button("Nochmal versuchen", self.start_scan, primary=True).pack(fill="x")
            return
        self.scanning = True
        self.set_text("WLAN beitreten", "Halte einen WLAN-QR-Code vor die Kamera.")
        self.button("Abbrechen", self.show_share).pack(fill="x")
        self.after(30, self.scan_frame)

    def scan_frame(self):
        if not self.scanning or self.cam is None:
            return
        ok, frame = self.cam.read()
        if ok:
            data, _, _ = self.detector.detectAndDecode(frame)
            self.cam_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            self.refresh_image()
            if data:
                info = parse_wifi_qr(data)
                if info:
                    self.stop_scan()
                    self.ask_join(info)
                    return
                self.sub_lbl.configure(text="Das ist kein WLAN-QR-Code.", text_color=ERR_COLOR)
        self.after(30, self.scan_frame)

    def ask_join(self, info):
        self.clear_actions()
        self.cam_pil = None
        self.refresh_image()
        self.set_text(info["ssid"], "WLAN gefunden. Jetzt verbinden?")
        self.actions.columnconfigure((0, 1), weight=1, uniform="a")
        self.button("Abbrechen", self.show_share).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.button("Verbinden", lambda: self.join(info), primary=True).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def join(self, info):
        self.clear_actions()
        bar = ctk.CTkProgressBar(self.actions, mode="indeterminate", progress_color=ACCENT)
        bar.pack(fill="x")
        bar.start()

        def status(msg):
            self.after(0, lambda: self.sub_lbl.configure(text=msg, text_color=MUTED))

        def work():
            ok = connect_wifi(info, status)

            def done():
                self.clear_actions()
                if ok:
                    self.show_share()
                    self.sub_lbl.configure(text=f"Verbunden mit {info['ssid']}.", text_color=OK_COLOR)
                else:
                    self.set_text(info["ssid"], "Verbindung hat nicht geklappt. Ist das Netz in Reichweite "
                                                "und das Passwort richtig?", ERR_COLOR)
                    self.button("Nochmal scannen", self.start_scan, primary=True).pack(fill="x")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def stop_scan(self):
        self.scanning = False
        self.cam_pil = None
        if self.cam is not None:
            self.cam.release()
            self.cam = None

    def on_close(self):
        self.stop_scan()
        self.destroy()


if __name__ == "__main__":
    ctk.set_appearance_mode("system")
    App().mainloop()
