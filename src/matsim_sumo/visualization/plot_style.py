NETWORK_STYLE = {"color": "#6b7280", "linewidth": 0.18, "alpha": 0.75}
BOUNDARY_STYLE = {"color": "#111827", "linewidth": 1.25}
START_STYLE = {"color": "#0072B2", "markersize": 2.0, "alpha": 0.45}
END_STYLE = {"color": "#D55E00", "markersize": 2.0, "alpha": 0.45}


def apply_spatial_axes(ax, crs=None, legend: bool = True, **legend_kw) -> None:
    label = "CH1903+ / LV95" if crs and "2056" in str(crs) else str(crs or "projected CRS")
    ax.set_xlabel(f"Easting ({label}) [m]")
    ax.set_ylabel(f"Northing ({label}) [m]")
    ax.set_aspect("equal")
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.grid(color="#e5e7eb", linewidth=0.4)
    ax.set_axisbelow(True)
    if legend:
        ax.legend(loc="upper right", frameon=True, framealpha=0.95, **legend_kw)
