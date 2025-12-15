import io
from dataclasses import dataclass
import numpy as np
import numpy_financial as npf
import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows

st.set_page_config(page_title="Cotizador DaaS", layout="wide")

DEFAULT_ITEMS = pd.DataFrame(
    [
        {"Tipo": "Laptop", "Nombre": "Laptop Perfil 1", "Cantidad": 10, "Costo_unit": 3000.0, "Spare_unit": 0.0},
    ]
)

def _as_float(x, default=0.0):
    try:
        if x is None or (isinstance(x, str) and x.strip() == ""):
            return float(default)
        return float(x)
    except Exception:
        return float(default)

def _as_int(x, default=0):
    try:
        if x is None or (isinstance(x, str) and x.strip() == ""):
            return int(default)
        return int(float(x))
    except Exception:
        return int(default)

@dataclass
class Params:
    plazo_meses: int
    tasa_objetivo: float   # retorno objetivo (mensual) para calcular canon
    tasa_capt: float       # fondeo (mensual)

    residual_rec_pct: float  # recuperación activo (ingreso final)
    residual_fon_pct: float  # balloon fondeo (egreso final)

    mantenimiento_pct: float
    seguros_pct: float
    provision_pct: float
    ica_pct: float
    renta_pct: float

def compute_items(items: pd.DataFrame) -> pd.DataFrame:
    df = items.copy()
    df["Cantidad"] = df["Cantidad"].apply(lambda v: max(_as_int(v, 0), 0))
    df["Costo_unit"] = df["Costo_unit"].apply(lambda v: max(_as_float(v, 0.0), 0.0))
    df["Spare_unit"] = df["Spare_unit"].apply(lambda v: max(_as_float(v, 0.0), 0.0))
    df["Costo_unit_total"] = df["Costo_unit"] + df["Spare_unit"]
    df["Costo_total"] = df["Costo_unit_total"] * df["Cantidad"]
    return df

def funding_payment(cost_equipos: float, p: Params):
    n = int(max(p.plazo_meses, 1))
    pv = float(max(cost_equipos, 0.0))
    residual = pv * float(max(p.residual_fon_pct, 0.0))
    fv = -residual
    if pv <= 0:
        return 0.0, 0.0
    pago = float(-npf.pmt(p.tasa_capt, n, pv, fv))
    return pago, residual

def cashflows_for_canon(canon: float, cost_equipos: float, p: Params) -> pd.DataFrame:
    n = int(max(p.plazo_meses, 1))
    canon = float(max(canon, 0.0))
    cost_equipos = float(max(cost_equipos, 0.0))

    pago_fondeo, residual_fondeo = funding_payment(cost_equipos, p)
    residual_rec = cost_equipos * float(max(p.residual_rec_pct, 0.0))

    rows = []
    for m in range(0, n + 1):
        if m == 0:
            rows.append({"Mes": 0, "Flujo_neto": -cost_equipos})
            continue

        cobro = canon
        cobro_res = residual_rec if m == n else 0.0

        pago = pago_fondeo
        pago_res = residual_fondeo if m == n else 0.0

        spread = (cobro + cobro_res) - (pago + pago_res)

        op = (cost_equipos * (p.mantenimiento_pct + p.seguros_pct) / n) if cost_equipos > 0 else 0.0
        prov = max(0.0, spread) * p.provision_pct
        ica = max(0.0, (cobro + cobro_res)) * p.ica_pct

        utilidad_ai = spread - op - prov - ica
        impuesto = max(0.0, utilidad_ai) * p.renta_pct
        flujo = utilidad_ai - impuesto

        rows.append({
            "Mes": m,
            "Cobro_cliente": cobro,
            "Cobro_residual_rec": cobro_res,
            "Pago_fondeo": pago,
            "Pago_residual_fon": pago_res,
            "Spread": spread,
            "Op_costos": op,
            "Provision": prov,
            "ICA": ica,
            "Utilidad_AI": utilidad_ai,
            "Impuesto": impuesto,
            "Flujo_neto": flujo,
        })

    return pd.DataFrame(rows)

def npv_monthly(rate: float, cashflows: np.ndarray) -> float:
    if len(cashflows) == 0:
        return 0.0
    return float(cashflows[0] + npf.npv(rate, cashflows[1:]))

def solve_canon(cost_equipos: float, p: Params):
    """Busca canon tal que NPV(tasa_objetivo)=0."""
    cost_equipos = float(max(cost_equipos, 0.0))
    if cost_equipos <= 0:
        cf = cashflows_for_canon(0.0, cost_equipos, p)
        return 0.0, cf, 0.0

    def f(c):
        cf = cashflows_for_canon(c, cost_equipos, p)
        arr = cf["Flujo_neto"].to_numpy(dtype=float)
        return npv_monthly(p.tasa_objetivo, arr)

    lo = 0.0
    hi = max(1.0, cost_equipos / max(p.plazo_meses, 1))
    f_hi = f(hi)

    guard = 0
    while f_hi <= 0 and guard < 60:
        hi *= 1.6
        f_hi = f(hi)
        guard += 1

    if f_hi <= 0:
        cf = cashflows_for_canon(hi, cost_equipos, p)
        arr = cf["Flujo_neto"].to_numpy(dtype=float)
        return hi, cf, npv_monthly(p.tasa_objetivo, arr)

    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if abs(f_mid) < 1e-6:
            lo = hi = mid
            break
        if f_mid > 0:
            hi = mid
        else:
            lo = mid

    canon = (lo + hi) / 2
    cf = cashflows_for_canon(canon, cost_equipos, p)
    arr = cf["Flujo_neto"].to_numpy(dtype=float)
    return canon, cf, npv_monthly(p.tasa_objetivo, arr)

def export_excel(items_calc: pd.DataFrame, cashflows: pd.DataFrame, params: Params, kpis: dict) -> bytes:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Items"
    ws2 = wb.create_sheet("Cashflows")
    ws3 = wb.create_sheet("Parametros")

    for r in dataframe_to_rows(items_calc, index=False, header=True):
        ws1.append(r)
    for r in dataframe_to_rows(cashflows, index=False, header=True):
        ws2.append(r)

    ws3.append(["Parametro", "Valor"])
    for k, v in params.__dict__.items():
        ws3.append([k, float(v)])
    ws3.append([])
    ws3.append(["KPIs", ""])
    for k, v in kpis.items():
        ws3.append([k, float(v) if isinstance(v, (int, float, np.floating)) else v])

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()

# ---------------- UI ----------------
st.title("Cotizador DaaS (canon mensual por ítem)")

with st.sidebar:
    st.header("Parámetros (mensuales)")
    plazo_meses = st.number_input("Plazo (meses)", min_value=1, max_value=120, value=36, step=1)

    colA, colB = st.columns(2)
    with colA:
        tasa_obj = st.number_input("Tasa objetivo negocio (mensual)", min_value=0.0, max_value=0.5, value=0.026, step=0.001, format="%.4f")
        tasa_capt = st.number_input("Tasa captación / fondeo (mensual)", min_value=0.0, max_value=0.5, value=0.006, step=0.001, format="%.4f")
    with colB:
        residual_rec_pct = st.number_input("Recuperación activo % (ingreso final)", min_value=0.0, max_value=0.9, value=0.15, step=0.01, format="%.4f")
        residual_fon_pct = st.number_input("Residual fondeo % (balloon egreso final)", min_value=0.0, max_value=0.9, value=0.15, step=0.01, format="%.4f")

    st.divider()
    st.subheader("Costos / riesgos")
    colC, colD = st.columns(2)
    with colC:
        mantenimiento_pct = st.number_input("Mantenimiento % (sobre costo equipos)", min_value=0.0, max_value=0.5, value=0.01, step=0.005, format="%.4f")
        seguros_pct = st.number_input("Seguros % (sobre costo equipos)", min_value=0.0, max_value=0.5, value=0.02, step=0.005, format="%.4f")
        provision_pct = st.number_input("Provisión % (sobre spread)", min_value=0.0, max_value=0.5, value=0.02, step=0.005, format="%.4f")
    with colD:
        ica_pct = st.number_input("ICA % (sobre cobro cliente)", min_value=0.0, max_value=0.2, value=0.01, step=0.001, format="%.4f")
        renta_pct = st.number_input("Impuesto renta %", min_value=0.0, max_value=0.8, value=0.35, step=0.01, format="%.4f")

    st.caption("Si tus tasas están en E.A., conviértelas a mensual: (1+EA)**(1/12)-1")

params = Params(
    plazo_meses=int(plazo_meses),
    tasa_objetivo=float(tasa_obj),
    tasa_capt=float(tasa_capt),
    residual_rec_pct=float(residual_rec_pct),
    residual_fon_pct=float(residual_fon_pct),
    mantenimiento_pct=float(mantenimiento_pct),
    seguros_pct=float(seguros_pct),
    provision_pct=float(provision_pct),
    ica_pct=float(ica_pct),
    renta_pct=float(renta_pct),
)

st.subheader("Ítems (agrega infinitos)")
if "items" not in st.session_state:
    st.session_state["items"] = DEFAULT_ITEMS.copy()

b1, b2, _ = st.columns([1, 1, 3])
with b1:
    if st.button("➕ Agregar fila"):
        st.session_state["items"] = pd.concat(
            [st.session_state["items"], pd.DataFrame([{"Tipo":"", "Nombre":"", "Cantidad":1, "Costo_unit":0.0, "Spare_unit":0.0}])],
            ignore_index=True
        )
with b2:
    if st.button("♻️ Reset"):
        st.session_state["items"] = DEFAULT_ITEMS.copy()

edited = st.data_editor(
    st.session_state["items"],
    num_rows="dynamic",
    use_container_width=True,
    hide_index=True,
    column_config={
        "Cantidad": st.column_config.NumberColumn(min_value=0, step=1),
        "Costo_unit": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%.2f"),
        "Spare_unit": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="%.2f"),
    },
)
st.session_state["items"] = edited

items_calc = compute_items(st.session_state["items"])

# ---- Canon por ítem (unitario) ----
canon_units = []
for _, row in items_calc.iterrows():
    unit_cost = float(row["Costo_unit_total"])
    if unit_cost <= 0:
        canon_units.append(0.0)
        continue
    canon_u, _, _ = solve_canon(unit_cost, params)   # goal-seek por unidad
    canon_units.append(canon_u)

items_calc["Canon_unit_mensual"] = canon_units
items_calc["Canon_total_mensual"] = items_calc["Canon_unit_mensual"] * items_calc["Cantidad"]

total_cost = float(items_calc["Costo_total"].sum())
total_canon = float(items_calc["Canon_total_mensual"].sum())

# Flujos agregados (equivalente a sumar flujos por item, por linealidad)
canon_calc, cashflows, npv_at_target = solve_canon(total_cost, params)
# Forzamos canon total como suma por item para mostrar consistencia visual
cashflows = cashflows_for_canon(total_canon, total_cost, params)
cf = cashflows["Flujo_neto"].to_numpy(dtype=float)
npv_at_target = npv_monthly(params.tasa_objetivo, cf)

pago_fondeo, residual_fon = funding_payment(total_cost, params)
residual_rec = total_cost * params.residual_rec_pct

irr_m = float(npf.irr(cf)) if (np.any(cf != 0) and len(cf) >= 2) else 0.0
irr_ea = (1.0 + irr_m) ** 12 - 1.0 if irr_m > -1 else float("nan")

kpis = {
    "Costo_total_equipos": total_cost,
    "Canon_total_mensual": total_canon,
    "Pago_mensual_fondeo": pago_fondeo,
    "NPV_a_tasa_objetivo": npv_at_target,
    "IRR_mensual": irr_m,
    "IRR_EA": irr_ea,
    "Residual_recuperacion_total": residual_rec,
    "Residual_fondeo_total": residual_fon,
}

st.subheader("Indicadores (total)")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Costo total equipos", f"${total_cost:,.0f}")
c2.metric("Canon total mensual", f"${total_canon:,.0f}")
c3.metric("Pago mensual fondeo", f"${pago_fondeo:,.0f}")
c4.metric("NPV a tasa objetivo", f"${npv_at_target:,.0f}")

st.caption("Nota: el canon unitario se calcula con goal-seek por unidad. El canon total es la suma: canon_unit * cantidad.")

st.divider()

tab1, tab2, tab3 = st.tabs(["Detalle Ítems (canon por ítem)", "Flujos de caja (total)", "Descargar Excel"])

with tab1:
    st.dataframe(items_calc, use_container_width=True)

with tab2:
    st.dataframe(cashflows, use_container_width=True)
    st.line_chart(cashflows.set_index("Mes")["Flujo_neto"].cumsum())

with tab3:
    xlsx_bytes = export_excel(items_calc, cashflows, params, kpis)
    st.download_button(
        "⬇️ Descargar Excel (Items + Cashflows + Parametros)",
        data=xlsx_bytes,
        file_name="cotizacion_daas_canon_por_item.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.caption("Si necesitas que algunos ítems tengan parámetros distintos (residual, mtto, seguro), te agrego columnas por ítem y el canon se recalcula con esos valores.")
