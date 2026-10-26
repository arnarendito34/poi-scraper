import pandas as pd
import re


def clean_competitor_data(input_file, output_file, removed_file, name_filters):

    # ==========================
    # 1. Membaca CSV
    # ==========================
    df = pd.read_csv(input_file)

    # ==========================
    # 2. Membersihkan nama dan kategori
    # ==========================
    name = df['Name_geocode'].fillna('').astype(str).str.strip()
    category = df['Category_geocode'].fillna('').astype(str).str.strip()

    # ==========================
    # 3. Nama wajib diawali nama kompetitor
    # ==========================
    patterns = []

    for competitor in name_filters:

        competitor = competitor.strip()

        # contoh:
        # Prima Mart
        # Prima-Mart
        # PrimaMart
        pattern = "^" + re.escape(competitor).replace(
            r"\ ",
            r"[\s-]*"
        ) + r"\b"

        patterns.append(pattern)

    combined_pattern = "|".join(patterns)

    valid_prefix = name.str.match(
        combined_pattern,
        case=False,
        na=False
    )

    # ==========================
    # 4. Blacklist kategori
    # (hanya untuk data bukan kompetitor)
    # ==========================
    # ==========================
# Blacklist nama
# ==========================
    blacklist_names = [
            'Jaya',
        ]

    name_pattern = '|'.join(
            re.escape(keyword)
            for keyword in blacklist_names
        )

    has_blacklist_name = name.str.contains(
            name_pattern,
            case=False,
            na=False,
            regex=True
        )
    blacklist_categories = [
        'Corporate office',
        'Bakery',
        'Bus stop',
        'ATM',
        'Dessert shop',
        'Coffee shop',
        'Tea store',
        'Parking lot',
        'Deli',
        'Vaporizer store',
        'Employment agency',
        'Restaurant',
        'Fast food restaurant',
        'Ice cream shop',
        'Coffee stand',
        'Air compressor supplier',
        'Mine',
        'Vegetable wholesaler',
        'Logistics service',
        'Company',
        'General contractor',
        'Public parking space',
        'Non-governmental organization',
        'Air conditioning store',
        'Construction company',
        'Pharmacy',
        'Vegetarian restaurant',
        'Night club',
        'Tea house',
        'Bubble tea store',
        'Juice shop',
        'Ranch',
        'Electronics store',
        'Office supply store',
        'Beverage distributor',
        'Pest control service',
        'Nasi goreng restaurant',
        'Porridge restaurant',
        'Auto machine shop',
        'Freight forwarding service',
        'Display stand manufacturer',
        'Hawker stall',
        'Distribution service',
        'Electric motorcycle dealer',
        'Health food store',
        'Saw mill',
        'Chocolate cafe',
        'Brunch restaurant',
        'Fish store',
        'Tofu shop',
        'Computer repair service',
        'Stationery store',
        'Feed manufacturer',
        'Cigar shop',
        'Bank',
        'Hawker center',
    ]

    category_pattern = "|".join(
        re.escape(keyword)
        for keyword in blacklist_categories
    )

    is_non_retail = category.str.contains(
        category_pattern,
        case=False,
        na=False,
        regex=True
    )


    # ==========================
    # 5. Alasan data dibuang
    # ==========================
    remove_reason = pd.Series("", index=df.index)

    remove_reason += (~valid_prefix).map(
        lambda x: "Nama kompetitor tidak sesuai; " if x else ""
    )

    remove_reason += (
        (is_non_retail & (~valid_prefix))
        .map(
            lambda x: "Kategori non retail; " if x else ""
        )
    )

    remove_reason = remove_reason.str.rstrip("; ")


    # ==========================
    # 6. Filter data
    # ==========================
    # Nama kompetitor adalah prioritas utama
    condition = valid_prefix


    df_cleaned = df.loc[condition].copy()

    df_removed = df.loc[~condition].copy()

    df_removed["Reason"] = remove_reason.loc[~condition]


    # ==========================
    # 7. Kolom output bersih
    # ==========================
    required_columns = [
        'Name_geocode',
        'Address_geocode',
        'Longitude_geocode',
        'Latitude_geocode',
    ]

    missing_columns = [
        col
        for col in required_columns
        if col not in df_cleaned.columns
    ]

    if missing_columns:
        raise ValueError(
            f"Kolom tidak ditemukan: {missing_columns}"
        )


    df_cleaned = df_cleaned[required_columns]


    # ==========================
    # 8. Simpan file
    # ==========================
    df_cleaned.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig"
    )

    df_removed.to_csv(
        removed_file,
        index=False,
        encoding="utf-8-sig"
    )


    # ==========================
    # 9. Ringkasan
    # ==========================
    print("\n===================================")
    print("Pembersihan data selesai!")
    print("===================================")
    print(f"Data awal      : {len(df)}")
    print(f"Data bersih    : {len(df_cleaned)}")
    print(f"Data dibuang   : {len(df_removed)}")
    print(f"Output bersih  : {output_file}")
    print(f"Output dibuang : {removed_file}")
    print("===================================")



# ==========================
# INPUT USER
# ==========================

input_filename = input(
    "Masukkan nama file CSV : "
).strip()


competitors = input(
    "Masukkan nama kompetitor (pisahkan dengan koma, titik koma, atau |) : "
).strip()


name_filters = [
    x.strip()
    for x in re.split(r"[;,|]", competitors)
    if x.strip()
]


output_filename = (
    input_filename.rsplit(".", 1)[0]
    + "_clean.csv"
)


removed_filename = (
    input_filename.rsplit(".", 1)[0]
    + "_removed.csv"
)


clean_competitor_data(
    input_filename,
    output_filename,
    removed_filename,
    name_filters
)