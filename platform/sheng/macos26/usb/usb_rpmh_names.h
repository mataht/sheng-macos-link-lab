/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Several drivers in the NativeUfs bundle carry their own copy of upstream's QcomRpmh.c: this driver's copy gets private symbol names. */
#ifndef SHENG_USB_RPMH_NAMES_H
#define SHENG_USB_RPMH_NAMES_H
#define qcom_cmd_db_find native_usb_qcom_cmd_db_find
#define qcom_rpmh_level_index native_usb_qcom_rpmh_level_index
#define qcom_rsc_layout native_usb_qcom_rsc_layout
#define qcom_rpmh_active_request native_usb_qcom_rpmh_active_request
#define qcom_rpmh_finish native_usb_qcom_rpmh_finish
#endif
