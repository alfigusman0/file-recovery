# FileRecovery

## Menjalankan aplikasi (antarmuka web)
Aplikasi berjalan sebagai server lokal di komputer Anda dan dibuka di browser. Tidak perlu internet, dan server hanya bisa diakses dari komputer ini.

```bash
python run_gui.pyw        # Windows: double-click
python -m recovery gui    # semua OS
```

Fitur di antarmuka:
- **Recycle Bin**: pulihkan file dari tempat sampah, dengan filter tanggal.
- **File dihapus**: cari file yang dihapus dengan nama asli, pratinjau gambar dan teks, pulihkan, ekspor CSV/JSON.
- **Carving**: pulihkan dari disk atau image dengan jenis file dan rentang tanggal.
- **Salin disk**: buat image dengan SHA-256.
- **Flashdisk**: diagnosa, uji permukaan, salin ke image, cek sistem file, dan perbaikan yang harus dikonfirmasi.
- **Perbaiki file**: PNG, JPEG, PDF, ZIP/Office, dan SQLite.
- **Mode debug** (sidebar bawah): panel log live dengan tahap pekerjaan, persen, kecepatan, sisa waktu, dan level log (DEBUG/INFO/WARNING/ERROR). Log bisa disalin.

Setiap pekerjaan menampilkan persen, kecepatan, dan estimasi sisa waktu. Pemindaian file dihapus menghitung jumlah folder dulu agar persen dan sisa waktunya akurat.

Keamanan: API hanya menerima permintaan dengan token acak yang dibuat saat server dijalankan, dan Host harus `127.0.0.1` atau `localhost`.


Aplikasi pemulihan file lintas platform (Windows, macOS, Linux) dengan antarmuka grafis dan baris perintah. Dibuat dengan Python.

## Instalasi
```bash
python -m pip install -r requirements.txt
```
`pytsk3` dibutuhkan untuk fitur "file dihapus dengan nama asli". Fitur lain tetap berjalan tanpanya.

## Menjalankan GUI
```bash
python run_gui.pyw        # Windows: double-click
python -m recovery.gui    # macOS / Linux / terminal
```

## Fitur

| Fitur | Keterangan |
|---|---|
| Recycle Bin / Trash | Lihat dan pulihkan file dari tempat sampah (Windows, Linux, macOS). Filter tanggal hapus. |
| File dihapus dengan nama asli | Membaca tabel file sistem (NTFS, FAT12/16/32, exFAT, ext, HFS+). Nama, folder, ukuran, dan perkiraan waktu hapus ikut dipulihkan. |
| Carving (tanpa metadata) | Mencari file berdasarkan signature untuk file yang metadatanya sudah hilang (format cepat, hapus permanen). 30+ format. |
| Filter tanggal | Rentang opsional. Kosong = semua file. Untuk carving, memakai tanggal yang tersimpan di dalam file (EXIF, PDF, ZIP). |
| Pilih disk otomatis | Daftar drive, partisi, dan disk fisik dari sistem. |
| Salin disk (image) | Salin disk ke file image sebelum pemulihan, dengan hash SHA-256 untuk verifikasi. Sektor rusak diisi nol dan dicatat. |
| Pengaman tujuan | Menolak menyimpan hasil ke disk/drive yang sedang dipindai. |
| Batal | Pemindaian dan salinan image bisa dibatalkan. File yang sudah tersimpan tetap ada. |
| Ekspor daftar | Daftar file dihapus diekspor ke CSV atau JSON. |
| Perbaikan file rusak | PNG, JPEG, PDF, ZIP/DOCX/XLSX/PPTX/APK, SQLite. File asli tidak diubah. |

## Cara pakai (CLI)
```bash
python -m recovery info
python -m recovery disks
python -m recovery trash list --from 2026-01-01 --to 2026-11-30
python -m recovery scan-deleted --source <disk_atau_image> --export daftar.csv
python -m recovery recover-deleted --source <disk_atau_image> --out ./hasil --from 2026-01-01 --to 2026-11-30 --match Docs
python -m recovery image --source <disk> --out ./disk_backup.img
python -m recovery carve --source <disk_atau_image> --out ./carved --ext jpg png pdf --from 2026-01-01
python -m recovery repair --path ./carved --out ./repaired
```

Sumber disk:
- Windows: `\\.\C:` atau `\\.\PhysicalDrive1` (jalankan sebagai Administrator)
- Linux: `/dev/sdb1` (jalankan dengan `sudo`)
- macOS: `/dev/disk2s1` (jalankan dengan `sudo`, beri izin Full Disk Access ke Terminal)

## Alur pemulihan yang disarankan
1. **Hentikan pemakaian** disk yang kehilangan data.
2. **Salin disk ke image** (tab "Salin disk" atau `image`). Pulihkan dari salinan, bukan dari disk asli.
3. **Cari file dihapus** dengan nama asli. Jika tidak ketemu, gunakan **carving**.
4. **Simpan hasil ke disk lain**, bukan ke disk yang dipulihkan.
5. **Perbaiki** file yang rusak jika diperlukan.

## Keterbatasan (jujur)
- **Waktu hapus** tidak selalu tersedia. NTFS tidak menyimpan waktu hapus, sehingga yang ditampilkan adalah perkiraan dari waktu perubahan catatan MFT. Untuk FAT, yang tersedia hanya waktu tulis terakhir.
- **Filter tanggal pada carving** memakai tanggal di dalam file (foto, PDF, ZIP), bukan tanggal hapus. File tanpa tanggal dilewati saat filter aktif.
- **Carving** tidak mengembalikan nama dan folder asli. File yang terfragmentasi belum utuh.
- **Disk terenkripsi** (BitLocker, FileVault, LUKS) harus dibuka dulu.
- **SSD dengan TRIM** sering menghapus data lama dengan cepat. Pemulihan tidak dijamin.
- **Perbaikan JPEG** bisa salah jika file memiliki thumbnail di dalamnya.
- **Preview gambar** di dalam aplikasi belum tersedia.
- Pengujian nyata dilakukan pada image FAT32 buatan (WSL). Pengujian pada NTFS, ext, dan disk asli di Windows, macOS, dan Linux masih perlu dilakukan.

## Membuat aplikasi mandiri (tanpa Python)
```bash
python -m pip install pyinstaller
python build_app.py
```
Hasil ada di folder `dist/`. Build dilakukan terpisah di setiap OS karena PyInstaller tidak melakukan cross-compile.

## Tes
```bash
python -m unittest discover -s tests -v
```

Penggunaan hanya untuk perangkat dan data yang Anda miliki atau punya izin untuk dipulihkan.
