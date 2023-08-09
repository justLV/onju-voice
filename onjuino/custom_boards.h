#pragma once

#ifdef BOARD_V1

#define BOARD_NAME "V1"
#define I2S_NUM I2S_NUM_0
#define I2S_BCK_PIN 40
#define I2S_WS_PIN 42
#define I2S_IN 41
#define I2S_OUT 17

#define MUTE 33

#define LED_PIN 48
#define LED_COUNT 6

#define SPEAKER_EN 47

#define T_L T1
#define T_C T2
#define T_R T3

#elif defined(BOARD_V2)

#define BOARD_NAME "V2"
#define I2S_NUM I2S_NUM_0
#define I2S_BCK_PIN 18
#define I2S_WS_PIN 13
#define I2S_IN 17
#define I2S_OUT 12

#define MUTE 38

#define LED_PIN 11
#define LED_COUNT 6

#define T_L T2
#define T_C T3
#define T_R T4

#define USE_PSRAM

#elif defined(BOARD_V3)

#define BOARD_NAME "V3"
#define I2S_NUM I2S_NUM_0
#define I2S_BCK_PIN 18
#define I2S_WS_PIN 13
#define I2S_IN 17
#define I2S_OUT 12

#define MUTE 38
#define SPEAKER_EN 21

#define LED_PIN 11
#define LED_COUNT 6

#define T_L T2
#define T_C T3
#define T_R T4

#define USE_PSRAM

#else
#error "No board defined!"
#endif
