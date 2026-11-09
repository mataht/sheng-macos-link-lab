// SPDX-License-Identifier: GPL-3.0-or-later
// Private link names for the imported I2C core and RPMh request builder. Every companion links into one kernel module, and other companions import
// the same upstream files, so the keyboard cover's copies must not export the upstream symbols. The imported files stay unchanged.
#ifndef SHENG_KEYBOARD_NAMES_H
#define SHENG_KEYBOARD_NAMES_H
#define geni_i2c_prepare keyboard_geni_i2c_prepare
#define geni_i2c_start_write keyboard_geni_i2c_start_write
#define geni_i2c_start_read keyboard_geni_i2c_start_read
#define geni_i2c_start_write_bytes keyboard_geni_i2c_start_write_bytes
#define geni_i2c_start_read_bytes keyboard_geni_i2c_start_read_bytes
#define geni_i2c_start_write_buffer keyboard_geni_i2c_start_write_buffer
#define geni_i2c_start_read_buffer keyboard_geni_i2c_start_read_buffer
#define geni_i2c_event keyboard_geni_i2c_event
#define geni_i2c_poll keyboard_geni_i2c_poll
#define geni_i2c_timeout keyboard_geni_i2c_timeout
#define qcom_cmd_db_find keyboard_cmd_db_find
#define qcom_rpmh_level_index keyboard_rpmh_level_index
#define qcom_rsc_layout keyboard_rsc_layout
#define qcom_rpmh_active_request keyboard_rpmh_active_request
#define qcom_rpmh_finish keyboard_rpmh_finish
#endif
