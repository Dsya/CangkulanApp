# Cangkulan — Android (Chaquopy)

Project ini membungkus `server_cangkulan.py` menjadi aplikasi Android
menggunakan Chaquopy. `MainActivity` menjalankan server Python di
`127.0.0.1:8000` di thread terpisah, lalu menampilkannya lewat WebView.

## Cara push ke GitHub (repo baru, bersih dari file lama)

1. Di GitHub, buat repo baru (kosong, jangan centang "Add README").
2. Di HP/laptop, extract zip ini, lalu dari dalam folder `CangkulanApp`:

```bash
git init
git add .
git commit -m "Initial commit: Cangkulan Android (Chaquopy)"
git branch -M main
git remote add origin https://github.com/USERNAME/NAMA_REPO_BARU.git
git push -u origin main
```

Kalau push dari HP tanpa terminal, cara lain: upload folder ini lewat
GitHub web ("Add file" → "Upload files") — drag semua isi folder
`CangkulanApp` (termasuk folder `.github` yang tersembunyi, pastikan
ikut terupload) ke halaman upload GitHub, lalu commit.

## Build APK otomatis

Begitu di-push, `.github/workflows/build-apk.yml` otomatis build APK.
Ambil hasilnya: tab **Actions** → run terbaru → **Artifacts** →
download `cangkulan-debug-apk`.

## Struktur

```
CangkulanApp/
├── build.gradle              # AGP 8.5.2, Chaquopy 17.0.0
├── settings.gradle
├── app/
│   ├── build.gradle          # sudah termasuk fix duplicate kotlin-stdlib
│   ├── src/main/
│   │   ├── AndroidManifest.xml
│   │   ├── java/com/rj/cangkulan/MainActivity.java
│   │   ├── python/server_cangkulan.py + bgm4.mp3
│   │   └── res/ (values, mipmap-*)
└── .github/workflows/build-apk.yml
```
