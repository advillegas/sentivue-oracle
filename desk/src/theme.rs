//! The Claude Code look: warm near-black, Claude-orange accents, monospace
//! everything, rounded bordered widgets. Terminal soul, native body.

use eframe::egui::{self, Color32, FontFamily, FontId, Rounding, Stroke, TextStyle, Vec2};

// Cursor design language: layered cool grays, blue accent, sans-serif UI,
// monospace only for code-like content. (ORANGE/CORAL keep their names for
// source compatibility but now carry the accent blues.)
pub const BG: Color32 = Color32::from_rgb(31, 31, 36);
pub const BG_DARK: Color32 = Color32::from_rgb(23, 23, 27);
pub const BG_WIDGET: Color32 = Color32::from_rgb(38, 38, 44);
pub const BG_HOVER: Color32 = Color32::from_rgb(46, 46, 53);
pub const BORDER: Color32 = Color32::from_rgb(43, 43, 50);
pub const ORANGE: Color32 = Color32::from_rgb(79, 142, 247);   // accent blue
pub const CORAL: Color32 = Color32::from_rgb(110, 163, 249);   // lighter accent
pub const BG_DEEP: Color32 = BG_DARK;
pub const TEXT: Color32 = Color32::from_rgb(214, 214, 221);
pub const DIM: Color32 = Color32::from_rgb(133, 133, 143);
pub const GREEN: Color32 = Color32::from_rgb(76, 175, 112);
pub const RED: Color32 = Color32::from_rgb(241, 97, 97);

pub fn apply(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.text_styles = [
        (TextStyle::Heading, FontId::new(16.0, FontFamily::Proportional)),
        (TextStyle::Body, FontId::new(13.5, FontFamily::Proportional)),
        (TextStyle::Monospace, FontId::new(12.5, FontFamily::Monospace)),
        (TextStyle::Button, FontId::new(13.5, FontFamily::Proportional)),
        (TextStyle::Small, FontId::new(11.0, FontFamily::Proportional)),
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
