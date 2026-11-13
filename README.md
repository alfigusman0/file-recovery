# FileRecovery

<!-- Badge CI: ganti OWNER/REPO dengan nama repositori GitHub Anda. -->
[![tests](https://github.com/OWNER/REPO/actions/workflows/tests.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/tests.yml)

Aplikasi untuk memulihkan file yang terhapus dan memeriksa flashdisk atau kartu memori.
Berjalan di Windows, macOS, dan Linux. Tampilannya dibuka di browser, tetapi semuanya
berjalan di komputer Anda sendiri: tidak perlu internet dan tidak ada data yang dikirim keluar.

> Gunakan hanya untuk perangkat dan data milik Anda, atau yang Anda punya izin untuk dipulihkan.

---

## Daftar isi
1. [Aturan emas sebelum mulai](#1-aturan-emas-sebelum-mulai)
2. [Instalasi](#2-instalasi)
3. [Menjalankan aplikasi](#3-menjalankan-aplikasi)
4. [Hak Administrator / root](#4-hak-administrator--root)
5. [Fitur](#5-fitur)
6. [Pengaman bawaan](#6-pengaman-bawaan)
7. [Keterbatasan yang diketahui](#7-keterbatasan-yang-diketahui)
8. [Baris perintah (CLI)](#8-baris-perintah-cli)
9. [Membuat aplikasi mandiri](#9-membuat-aplikasi-mandiri-tanpa-python)
10. [Tes dan CI](#10-tes-dan-ci)

---

## 1. Aturan emas sebelum mulai
1. **Berhenti memakai** disk atau flashdisk yang kehilangan data. Setiap file baru bisa menimpa file yang ingin dikembalikan.
2. **Salin dulu ke file image** (menu *Salin disk*), lalu pulihkan dari image tersebut, bukan dari perangkat aslinya.
3. **Simpan hasil ke disk lain.** Aplikasi menolak jika tujuan berada di disk yang sama dengan sumber.
4. **Perbaikan sistem file paling terakhir**, setelah data penting sudah aman.

## 2. Instalasi
Butuh Python 3.12 atau lebih baru (versi 3.12 dan 3.13 diuji otomatis).

```bash
python -m pip install -r requirements.txt
```

Paket yang dipasang hanya `pytsk3` (The Sleuth Kit). Paket ini dibutuhkan untuk mencari file
dihapus dengan nama aslinya dan untuk mengenali sistem file saat diagnosa. Fitur lain tetap
berjalan tanpa `pytsk3`.

## 3. Menjalankan aplikasi
```bash
python run_gui.pyw        # Windows: bisa juga klik dua kali file run_gui.pyw
python -m recovery gui    # semua OS
```

Browser akan terbuka di alamat `http://127.0.0.1:8765/` (port lain dipakai otomatis jika 8765 sedang terpakai).
Biarkan jendela terminal tetap terbuka selama aplikasi dipakai. Tekan `Ctrl+C` di terminal untuk berhenti.

Di pojok atas aplikasi terlihat sistem operasi dan status hak akses
(**Administrator** atau **bukan admin**).

## 4. Hak Administrator / root
Membaca disk atau flashdisk secara langsung (raw device) butuh hak khusus:

| OS | Cara menjalankan | Contoh sumber |
|---|---|---|
| Windows | Klik kanan Terminal/Command Prompt, pilih **Run as administrator**, lalu jalankan aplikasi | `\\.\E:` atau `\\.\PhysicalDrive1` |
| Linux | `sudo python3 -m recovery gui` | `/dev/sdb` atau `/dev/sdb1` |
| macOS | `sudo python3 -m recovery gui`, dan beri izin **Full Disk Access** ke Terminal (System Settings > Privacy & Security) | `/dev/disk2` atau `/dev/disk2s1` |

Tanpa hak ini, Anda tetap bisa memakai Recycle Bin, memindai **file image**, dan memperbaiki file.

## 5. Fitur

### Recycle Bin / Trash
Melihat dan memulihkan file dari tempat sampah Windows, macOS, dan Linux. Bisa disaring menurut tanggal hapus.

### File dihapus (dengan nama asli)
Membaca tabel sistem file (NTFS, FAT12/16/32, exFAT, ext, HFS+) lewat `pytsk3`. Nama, folder, ukuran,
dan perkiraan waktu hapus ikut ditampilkan. Ada pratinjau gambar dan teks, ekspor daftar ke CSV/JSON,
dan pemulihan file yang dipilih.

### Carving (tanpa nama file)
Mencari file berdasarkan isinya, untuk kasus format cepat atau hapus permanen ketika tabel sistem file
sudah hilang. Jenis file: JPG, PNG, GIF, PDF, ZIP (termasuk DOCX/XLSX/PPTX/APK), RAR, 7Z, GZ, SQLite,
MP4, MOV, AVI, WAV, MP3, dan EXE. Bisa dibatasi dengan rentang tanggal yang tersimpan di dalam file
(EXIF foto, PDF, ZIP).

**Validator.** Tanda awal file yang pendek (misalnya `MZ` pada EXE atau `ftyp` pada MP4) sering muncul
secara kebetulan di data acak. Karena itu beberapa format diperiksa lebih jauh sebelum disimpan:
header JPEG, header PE untuk EXE, kotak `ftyp` dan struktur kotak MP4/MOV, frame audio MP3,
tag ID3, dan header GZIP. Kandidat yang gagal diperiksa dibuang.

**Tingkat keyakinan** di tabel hasil:

| Tingkat | Arti |
|---|---|
| **Tinggi** | File lengkap dan strukturnya dikonfirmasi oleh validator. Kemungkinan besar bisa dibuka normal. |
| **Sedang** | Penanda akhir file ditemukan, tetapi isinya tidak diperiksa mendalam. Mungkin ada sedikit kerusakan. |
| **Rendah** | Akhir file hanya ditebak (tidak ada penanda akhir, dipotong di batas ukuran atau di file berikutnya). Isinya bisa terpotong atau tercampur data lain. |

### Salin disk (image)
Menyalin seluruh disk atau partisi ke satu file image, dengan hash SHA-256 untuk verifikasi.
Sektor yang tidak bisa dibaca diisi nol dan dicatat.

### Halaman Flashdisk
Untuk flashdisk, kartu memori, dan perangkat yang RAW atau tidak terbaca. Ikuti urutan nomornya:

1. **Diagnosa**: membaca sektor awal dan mengenali sistem file. Hasilnya: sistem file terbaca, RAW (sektor awal kosong), atau rusak/tidak dikenali, beserta saran langkah berikutnya. Tidak menulis apa pun.
2. **Uji permukaan**: membaca semua sektor satu per satu dan mencatat sektor yang tidak terbaca. Tidak menulis dan tidak "memperbaiki" sektor.
3. **Salin ke image**: cadangan sebelum perbaikan apa pun.
4. **Pulihkan dari image**: langsung membuka halaman Carving dengan image tersebut.
5. **Cek sistem file (aman)**: hanya memeriksa. Windows `chkdsk` tanpa `/f`, Linux `fsck -n`, macOS `diskutil verifyVolume`.
6. **Perbaiki sistem file**: *menulis ke perangkat*. Harus dikonfirmasi dulu, dan ditolak untuk drive sistem.

### Uji kapasitas (deteksi flashdisk palsu)
Flashdisk palsu mengaku berkapasitas besar (misalnya 64 GB) padahal chip aslinya kecil. Uji ini
menulis data uji dengan pola yang diketahui ke folder `capacity_test` di drive yang diuji,
membacanya kembali, lalu membandingkan setiap blok 1 MB. Jika data mulai berbeda di titik tertentu,
aplikasi melaporkan "kemungkinan flashdisk palsu" dan di sekitar berapa GB kerusakan dimulai.

Perhatikan sebelum menjalankan:
- Uji ini **menulis ke ruang kosong** drive dan **menimpa isi ruang kosong** tersebut. Jangan jalankan uji ini pada drive yang masih berisi file terhapus yang ingin Anda pulihkan.
- Drive akan hampir penuh selama uji. File yang sudah ada tidak diubah, tetapi pada flashdisk palsu, penulisan melewati kapasitas asli bisa merusak file yang sudah ada. **Pindahkan data penting dulu.**
- Folder `capacity_test` selalu dihapus di akhir uji, juga jika dibatalkan atau gagal.
- Ditolak untuk drive sistem dan untuk path perangkat mentah (harus berupa folder, misalnya `G:\`).

### Perlindungan perangkat macet ("tidak merespons")
Flashdisk yang rusak atau palsu kadang berhenti menjawab, dan pembacaan bisa tertahan lama di sistem
operasi. Aplikasi membatasi waktu tunggu: sekitar 8 detik untuk pemeriksaan cepat dan 30 detik untuk
setiap blok saat uji permukaan. Jika batas terlewati, perangkat ditandai **tidak merespons** di daftar
disk, tombol operasi untuk perangkat itu dinonaktifkan sementara (sekitar 2 menit), dan aplikasi tetap
bisa dipakai untuk hal lain. Solusinya: cabut perangkat, colokkan lagi, lalu muat ulang daftar disk.

### Filter hasil
Tabel hasil **File dihapus** dan **Carving** punya filter: cari nama/path, jenis file,
ukuran minimum dan maksimum (MB), dan status (Utuh / Sebagian). Ada tombol **Reset filter** dan
keterangan "menampilkan X dari Y".

### Perbaiki file rusak
PNG, JPEG, PDF, ZIP/DOCX/XLSX/PPTX/APK, dan SQLite. File asli tidak diubah; hasil perbaikan disimpan di folder lain.

### Mode debug
Aktifkan lewat **Mode debug** di bagian bawah menu samping. Panel log tampil langsung dengan tahap
pekerjaan, persen, kecepatan, dan sisa waktu. Log bisa disaring per level (DEBUG/INFO/WARNING/ERROR)
dan disalin untuk dilaporkan. Log juga disimpan di file `FileRecovery/logs/recovery.log` di folder home Anda.

Setiap pekerjaan panjang menampilkan persen, kecepatan, dan perkiraan sisa waktu, serta punya tombol **Batal**.

## 6. Pengaman bawaan
- **Tujuan di drive yang sama ditolak.** Jika sumber adalah disk/perangkat, hasil tidak boleh disimpan ke disk yang sama. Jika aplikasi tidak bisa memastikan tujuan berada di disk lain, tujuan juga ditolak.
- **Perbaikan ditolak untuk drive sistem** (Windows: drive sistem seperti `C:`, macOS: `disk0`, Linux: `/dev/sda`). Uji kapasitas juga ditolak di drive sistem.
- **Format tidak pernah ditawarkan.** Format menghapus isi perangkat, jadi fitur ini sengaja tidak ada.
- Server hanya mendengarkan di `127.0.0.1`, dan setiap permintaan harus membawa token acak yang dibuat saat aplikasi dijalankan.

## 7. Keterbatasan yang diketahui
- **Perilaku pada NTFS, exFAT, ext4, dan APFS di media asli belum diverifikasi.** Tes otomatis memakai image FAT buatan. APFS tidak ada dalam daftar sistem file yang dikenali pemindaian nama asli.
- **File dihapus di ext4 tidak punya nama.** Hasil dari ext4 muncul tanpa nama asli. Gunakan carving untuk ext4.
- **Masalah pada tombol Batal belum terdiagnosis.** Ada laporan bahwa pembatalan tidak selalu berjalan seperti yang diharapkan, dan penyebabnya belum diketahui. File yang sudah tersimpan sebelum dibatalkan tetap ada.
- **Carving masih bisa menghasilkan file palsu (false positive).** Validator mengurangi jumlahnya, tetapi tidak menghilangkannya. Periksa hasil dengan tingkat keyakinan Rendah.
- **Waktu hapus hanya perkiraan.** NTFS tidak menyimpan waktu hapus; yang dipakai adalah waktu perubahan catatan MFT. FAT hanya punya waktu tulis terakhir.
- **Filter tanggal pada carving** memakai tanggal di dalam file, bukan tanggal hapus. File tanpa tanggal dilewati saat filter aktif.
- **Carving tidak mengembalikan nama dan folder asli**, dan file yang terpecah-pecah (fragmentasi) tidak bisa utuh.
- **Disk terenkripsi** (BitLocker, FileVault, LUKS) harus dibuka dulu.
- **SSD dengan TRIM** sering menghapus data lama dengan cepat. Pemulihan tidak dijamin.

## 8. Baris perintah (CLI)
Untuk pengguna yang terbiasa dengan terminal:

```bash
python -m recovery info
python -m recovery disks
python -m recovery trash list --from 2026-01-01 --to 2026-11-30
python -m recovery scan-deleted --source <disk_atau_image> --export daftar.csv
python -m recovery recover-deleted --source <disk_atau_image> --out ./hasil --match Docs
python -m recovery image --source <disk> --out ./disk_backup.img
python -m recovery carve --source <disk_atau_image> --out ./carved --ext jpg png pdf --from 2026-01-01
python -m recovery repair --path ./carved --out ./repaired
```

## 9. Membuat aplikasi mandiri (tanpa Python)
```bash
python -m pip install -r requirements.txt
python -m pip install pyinstaller
python build_app.py            # lihat juga: python build_app.py --help
```

Hasil ada di folder `dist/`:
- Windows: `FileRecovery.exe` (satu file, tanpa jendela konsol)
- macOS: `FileRecovery.app`
- Linux: `FileRecovery` (satu file, jalankan dari terminal)

PyInstaller tidak bisa membuat aplikasi untuk OS lain: build di setiap OS secara terpisah.

Perintah PyInstaller yang dijalankan `build_app.py` kira-kira seperti ini:

```bash
# Windows (pemisah --add-data adalah ;)
# python -m PyInstaller --noconfirm --clean --name FileRecovery --onefile --windowed ^
#   --add-data "recovery/webapp/static;recovery/webapp/static" --collect-all pytsk3 run_gui.pyw
#
# macOS (.app)
# python3 -m PyInstaller --noconfirm --clean --name FileRecovery --onedir --windowed \
#   --osx-bundle-identifier id.ac.uinsgd.filerecovery \
#   --add-data "recovery/webapp/static:recovery/webapp/static" --collect-all pytsk3 run_gui.pyw
#
# Linux (satu file)
# python3 -m PyInstaller --noconfirm --clean --name FileRecovery --onefile \
#   --add-data "recovery/webapp/static:recovery/webapp/static" --collect-all pytsk3 run_gui.pyw
```

Aplikasi hasil build tetap butuh hak Administrator/root untuk membaca disk secara langsung (lihat bagian 4).

## 10. Tes dan CI
```bash
python -m unittest discover -s tests -v
```

GitHub Actions (`.github/workflows/tests.yml`) menjalankan tes yang sama di Ubuntu, macOS, dan Windows
dengan Python 3.12 dan 3.13. Beberapa tes hanya berlaku di Windows atau Linux dan otomatis dilewati
(skip) di OS lain. Jika tes gagal, keluaran tes diunggah sebagai artifact.
