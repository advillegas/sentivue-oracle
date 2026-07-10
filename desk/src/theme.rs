//! The Claude Code look: warm near-black, Claude-orange accents, monospace
//! everything, rounded bordered widgets. Terminal soul, native body.

use eframe::egui::{self, Color32, FontFamily, FontId, Rounding, Stroke, TextStyle, Vec2};

pub const BG: Color32 = Color32::from_rgb(38, 37, 33);
pub const BG_DARK: Color32 = Color32::from_rgb(30, 29, 26);
pub const BG_WIDGET: Color32 = Color32::from_rgb(50, 48, 42);
pub const BG_HOVER: Color32 = Color32::from_rgb(62, 59, 51);
pub const BORDER: Color32 = Color32::from_rgb(72, 68, 59);
pub const ORANGE: Color32 = Color32::from_rgb(217, 119, 87);
pub const CORAL: Color32 = Color32::from_rgb(226, 139, 106);
pub const BG_DEEP: Color32 = BG_DARK;
pub const TEXT: Color32 = Color32::from_rgb(232, 227, 214);
pub const DIM: Color32 = Color32::from_rgb(150, 143, 128);
pub const GREEN: Color32 = Color32::from_rgb(140, 178, 120);
pub const RED: Color32 = Color32::from_rgb(203, 102, 90);

pub fn apply(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.text_styles = [
        (TextStyle::Heading, FontId::new(17.0, FontFamily::Monospace)),
        (TextStyle::Body, FontId::new(13.5, FontFamily::Monospace)),
        (TextStyle::Monospace, FontId::new(13.5, FontFamily::Monospace)),
        (TextStyle::Button, FontId::new(13.5, FontFamily::Monospace)),
        (TextStyle::Small, FontId::new(11.0, FontFamily::Monospace)),
    ]
    .into();
    style.spacing.item_spacing = Vec2::new(8.0, 7.0);
    style.spacing.button_padding = Vec2::new(12.0, 7.0);

    let mut v = egui::Visuals::dark();
    v.override_text_color = Some(TEXT);
    v.panel_fill = BG;
    v.window_fill = BG;
    v.extreme_bg_color = BG_DARK;
    v.faint_bg_color = BG_DARK;
    v.widgets.noninteractive.bg_fill = BG;
    v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, BORDER);
    v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, TEXT);
    v.widgets.inactive.bg_fill = BG_WIDGET;
    v.widgets.inactive.weak_bg_fill = BG_WIDGET;
    v.widgets.inactive.fg_stroke = Stroke::new(1.0, TEXT);
    v.widgets.inactive.rounding = Rounding::same(6.0);
    v.widgets.hovered.bg_fill = BG_HOVER;
    v.widgets.hovered.weak_bg_fill = BG_HOVER;
    v.widgets.hovered.bg_stroke = Stroke::new(1.0, ORANGE);
    v.widgets.hovered.rounding = Rounding::same(6.0);
    v.widgets.active.bg_fill = BG_HOVER;
    v.widgets.active.weak_bg_fill = BG_HOVER;
    v.widgets.active.bg_stroke = Stroke::new(1.0, ORANGE);
    v.widgets.active.rounding = Rounding::same(6.0);
    v.selection.bg_fill = ORANGE.linear_multiply(0.35);
    v.selection.stroke = Stroke::new(1.0, ORANGE);
    v.hyperlink_color = ORANGE;
    v.window_rounding = Rounding::same(8.0);
    v.window_stroke = Stroke::new(1.0, BORDER);
    ctx.set_style(style);
    ctx.set_visuals(v);
}
