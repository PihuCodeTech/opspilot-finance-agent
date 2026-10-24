"""Products the synthetic vendors sell, with the ways invoices describe them.

Each item has its PO wording plus two paraphrase sets. Set A is used in every
split; set B appears only in the final evaluation set (held-out paraphrases,
`04-prd.md` D6), so wording the agent was tuned on is never the only wording
it is tested on.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    sku: str
    po_description: str
    paraphrases_a: tuple[str, ...]
    paraphrases_b: tuple[str, ...]
    unit_price: Decimal
    typical_quantity: tuple[int, int]


def _p(  # noqa: PLR0917  # a compact table row
    sku: str, po: str, a: tuple[str, ...], b: tuple[str, ...], price: str, qty: tuple[int, int]
) -> Product:
    return Product(sku, po, a, b, Decimal(price), qty)


CATALOG: tuple[Product, ...] = (
    _p(
        "HB-M8-40",
        "Hex bolt M8 x 40mm, zinc plated",
        ("Hex bolts M8x40 zinc", "Bolt, hex, M8 x 40, ZP"),
        ("M8 hexagon bolts 40 mm galvanised", "Zinc hex screw M8-40"),
        "0.4200",
        (200, 2000),
    ),
    _p(
        "WN-M8",
        "Washer, flat, M8, stainless",
        ("Flat washers M8 SS", "M8 washer stainless flat"),
        ("Stainless plain washer size 8", "Washer M8 A2 flat"),
        "0.0900",
        (500, 5000),
    ),
    _p(
        "NL-M8",
        "Nyloc nut M8, zinc",
        ("M8 nylon lock nuts", "Nut nyloc M8 ZP"),
        ("Self-locking nut M8", "Insert lock nut, metric 8"),
        "0.1500",
        (500, 4000),
    ),
    _p(
        "GL-NIT-L",
        "Nitrile gloves, large, box of 100",
        ("Gloves nitrile L (100)", "Nitrile disposable gloves size L"),
        ("Box of 100 large nitrile gloves", "Exam gloves nitrile, L, 100/bx"),
        "8.5000",
        (10, 120),
    ),
    _p(
        "SG-CLR",
        "Safety glasses, clear lens",
        ("Clear safety glasses", "Safety spectacles, clear"),
        ("Protective eyewear clear", "Glasses, safety, clear lens EN166"),
        "4.2500",
        (10, 80),
    ),
    _p(
        "CB-CAT6-305",
        "Cat6 cable, 305m reel, blue",
        ("CAT6 UTP 305m blue", "Network cable Cat 6 reel 305 m"),
        ("Blue category-6 cable drum 305m", "Ethernet cable cat6, 305 m"),
        "142.0000",
        (1, 12),
    ),
    _p(
        "PL-EUR-120",
        "Euro pallet 1200x800, heat treated",
        ("EUR pallet 120x80 HT", "Pallet euro HT 1200x800"),
        ("Heat-treated pallet 1200 by 800", "Wooden euro pallet ISPM15"),
        "18.7500",
        (10, 200),
    ),
    _p(
        "SW-ST-50",
        "Stretch wrap 500mm x 300m",
        ("Stretch film 500mm 300m", "Pallet wrap 50cm x 300m"),
        ("Cling film for pallets 500 mm", "Wrap, stretch, 300 m roll"),
        "12.4000",
        (6, 120),
    ),
    _p(
        "TN-HP-58A",
        "Toner cartridge HP 58A, black",
        ("HP 58A toner black", "Toner, HP CF258A"),
        ("Black laser toner 58A", "Printer cartridge HP-58A"),
        "96.0000",
        (2, 24),
    ),
    _p(
        "PP-A4-80",
        "Copy paper A4 80gsm, box of 5 reams",
        ("A4 paper 80g (5 reams)", "Paper A4 80 gsm box/5"),
        ("Printer paper A4, 2500 sheets", "Office paper A4 80g, carton"),
        "24.9000",
        (5, 60),
    ),
    _p(
        "LB-LED-18",
        "LED tube T8 18W, 4000K",
        ("T8 LED tube 18W", "LED lamp T8 18 W neutral"),
        ("Fluorescent replacement LED 18W", "Tube light LED T8 4000K"),
        "6.8000",
        (20, 300),
    ),
    _p(
        "CL-DEG-5",
        "Industrial degreaser, 5L",
        ("Degreaser 5 litre", "Industrial cleaner degreasing 5L"),
        ("Heavy-duty degreasing fluid 5 l", "Cleaning solvent, degreaser, 5L can"),
        "21.5000",
        (4, 60),
    ),
    _p(
        "FL-OIL-46",
        "Hydraulic oil ISO 46, 20L",
        ("Hydraulic oil 46 20L", "ISO VG46 hydraulic fluid 20 l"),
        ("Hydraulic fluid grade 46, drum 20L", "Oil, hydraulic, VG 46"),
        "68.0000",
        (2, 40),
    ),
    _p(
        "BR-6205",
        "Ball bearing 6205-2RS",
        ("Bearing 6205 2RS", "6205-2RS deep groove bearing"),
        ("Sealed ball bearing 6205", "Deep-groove bearing 25x52x15"),
        "5.6000",
        (10, 200),
    ),
    _p(
        "VB-A42",
        "V-belt A42",
        ("V belt A42", "Belt, V, section A, 42in"),
        ("Classic V-belt A-42", "Drive belt A42"),
        "7.9000",
        (5, 60),
    ),
    _p(
        "SV-FIRE-6",
        "Fire extinguisher service, 6kg ABC",
        ("Extinguisher service 6kg", "Service of ABC extinguisher 6 kg"),
        ("Annual check, powder extinguisher 6kg", "Fire extinguisher maintenance 6kg"),
        "35.0000",
        (2, 30),
    ),
    _p(
        "SV-CLN-HR",
        "Cleaning services, per hour",
        ("Cleaning labour (hours)", "Contract cleaning hourly"),
        ("Janitorial service per hour", "Office cleaning, hourly rate"),
        "28.0000",
        (20, 200),
    ),
    _p(
        "SV-HVAC-V",
        "HVAC maintenance visit",
        ("HVAC service visit", "Maintenance call-out HVAC"),
        ("Air conditioning service visit", "Heating/ventilation maintenance"),
        "185.0000",
        (1, 12),
    ),
    _p(
        "LT-TONER-W",
        "Waste toner container",
        ("Toner waste box", "Waste toner bottle"),
        ("Used toner collector", "Container for waste toner"),
        "18.0000",
        (2, 20),
    ),
    _p(
        "MS-WRL",
        "Wireless mouse, black",
        ("Mouse wireless blk", "Cordless mouse black"),
        ("Black wireless computer mouse", "Mouse, 2.4GHz wireless"),
        "14.5000",
        (5, 80),
    ),
)

# Typical high-value products, used to build PO totals near approval-level boundaries.
HIGH_VALUE: tuple[Product, ...] = (
    _p(
        "SRV-R760",
        "Rack server, 2U, dual CPU",
        ("2U rack server", "Server, rack mount 2U"),
        ("Dual-socket rack server 2U", "Datacentre server 2U"),
        "8450.0000",
        (1, 6),
    ),
    _p(
        "FL-LIFT-25",
        "Electric pallet truck 2.5t",
        ("Pallet truck electric 2.5 t", "Powered pallet jack 2500kg"),
        ("Electric pallet mover 2.5 tonne", "Pallet truck, battery, 2.5t"),
        "4980.0000",
        (1, 8),
    ),
    _p(
        "CMP-AIR-15",
        "Air compressor 15kW",
        ("Compressor air 15 kW", "15kW screw compressor"),
        ("Screw air compressor, 15 kW", "Industrial compressor 15kW"),
        "11200.0000",
        (1, 3),
    ),
)
