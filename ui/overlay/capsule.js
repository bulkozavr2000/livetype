/*
 * LiveType dictation capsule: WebGL2 "Siri" glass renderer.
 *
 * Ported from Type (GigaType2, https://github.com/mishanaer/GigaType2), a fork of OpenWhispr.
 * Both are MIT-licensed. Copyright (c) the OpenWhispr and Type authors.
 *
 * Source: Type 3.1.0, app.asar -> src/dist/assets/index-CdfEpVi_.js, the module that starts at the first
 * shader (`lt=`#version 300 es`) and ends with `yn()`: shaders, uniform builders `_t`/`vt`, the renderer
 * class `Lt`, the spring `Wt`, the state machine `_n()`, `vn` (silent levels) and `yn(level)`.
 * That code is kept as it was in the bundle (minified names included); only line breaks and indentation
 * were added. The six shaders are byte-for-byte identical to the original.
 *
 * Type's React component `bn` (the rAF driver) is replaced by the plain-JS `CapsuleView` at the end of
 * this file, which reproduces its effects one by one.
 *
 * MIT License: Permission is hereby granted, free of charge, to any person obtaining a copy of this
 * software and associated documentation files (the "Software"), to deal in the Software without
 * restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute,
 * sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished
 * to do so, subject to the following conditions: The above copyright notice and this permission notice
 * shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED
 * "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.
 */
/* eslint-disable */
(function () {
"use strict";

// ---------------------------------------------------------------------------------------------------
// Ported from Type (verbatim apart from whitespace)
// ---------------------------------------------------------------------------------------------------
var lt=`#version 300 es
precision highp float;
const vec2 POSITIONS[3] = vec2[3](
  vec2(-1.0, -1.0), vec2(3.0, -1.0), vec2(-1.0, 3.0)
);
void main() { gl_Position = vec4(POSITIONS[gl_VertexID], 0.0, 1.0); }
`,ut=`#version 300 es
precision highp float;

uniform vec2 uResolution;
uniform float uTime;
uniform vec4 uMouse;
uniform float uResolved;
uniform float uLayerOpacity;
uniform float uUnresolvedScale;
uniform float uEffectScale;
uniform vec2 uAnchor;
uniform float uAmplitude;
uniform float uFreq;
uniform float uAberrationFreq;
uniform float uWavePhase;
uniform float uWaveSpeed;
uniform float uWaveScale;
uniform float uAberration;
uniform float uThickness;
uniform float uIntensity;
uniform float uFalloff;
uniform float uEdgeMask;
uniform float uEdgeMaskInset;
uniform float uBandFill;
uniform float uBandFillThickness;
uniform float uSoftness;
uniform float uLow;
uniform float uMid;
uniform float uHigh;
uniform float uLowAmplitude;
uniform float uLowIntensity;
uniform float uBrightnessLow;
uniform float uAudioBrightnessResponse;
uniform float uMidAberration;
uniform float uMidAberrationAmplitude;
uniform float uMidBandFill;
uniform float uMidSoftness;
uniform float uHighAberration;
uniform float uHighAberrationAmplitude;
uniform float uWhiteClip;
uniform vec3 uWaveColor0;
uniform vec3 uWaveColor1;
uniform vec3 uWaveColor2;
uniform vec3 uWaveHighlightColor0;
uniform vec3 uWaveHighlightColor1;
uniform vec3 uWaveHighlightColor2;
uniform float uWaveCount;
out vec4 outColor;

float saturate(float value) { return clamp(value, 0.0, 1.0); }
vec3 wavePaletteColor(int index) {
  int paletteIndex = index % 3;
  if (paletteIndex == 0) return uWaveColor0;
  if (paletteIndex == 1) return uWaveColor1;
  return uWaveColor2;
}
vec3 waveHighlightColor(int index) {
  int paletteIndex = index % 3;
  if (paletteIndex == 0) return uWaveHighlightColor0;
  if (paletteIndex == 1) return uWaveHighlightColor1;
  return uWaveHighlightColor2;
}
float smoothUnit(float value) { return value * value * (3.0 - 2.0 * value); }

void main() {
  vec2 gid = vec2(gl_FragCoord.x, uResolution.y - gl_FragCoord.y);
  float tw = mod(uWavePhase, 62.831848) * uWaveSpeed;
  float lo = saturate(uLow);
  float md = saturate(uMid);
  float hi = saturate(uHigh);
  float res = saturate(uResolved);
  float c52 = uThickness * 0.01;
  float brightnessLow = saturate(uBrightnessLow);
  float c55 = (brightnessLow * uLowIntensity * uAudioBrightnessResponse + uIntensity) * 0.01;
  float c58 = max(0.0, md * uMidSoftness + uSoftness);
  float c61 = (md * uMidBandFill + uBandFill) * 0.0001;
  float c64 = (uLowAmplitude * 0.01) * lo + uAmplitude;
  float c68 = c64 + md * uMidAberrationAmplitude + hi * uHighAberrationAmplitude;
  float c72 = (md * uMidAberration + uAberration) + hi * uHighAberration;
  float c73 = c72 * res;
  float c76 = lo * 14.0;
  float c75 = md * 10.0 + 4.0;
  float n77 = mix(0.1, c52, res);
  float n78 = mix(0.1, c55, res);
  float n80 = (res * 0.01) * c58;
  float n81 = mix(c75, 1.0, res);
  float omr = 1.0 - res;

  vec2 uv = (gid + 0.5) * 2.0 / uResolution - 1.0;
  float aspect = uResolution.x / uResolution.y;
  uv.x *= aspect;
  vec2 q = uv - vec2(aspect, 1.0) * (uAnchor * 2.0 - 1.0);
  float ws = max(uWaveScale * uEffectScale, 0.01);
  vec2 p = q / ws;
  float base = mix(0.14, uUnresolvedScale, res);
  float r = length(p);
  float edge = max(r - base, 0.0);
  float aC = max(aspect, 1.0);
  float px = p.x / aC;
  float cw = min(abs(px * 0.9), 1.0);
  float cw2 = pow(cos(cw * 1.5707964), 2.0);
  float eps = 0.0001;
  float atArg = atan(px * eps) * aC / eps;
  float waveBase = (cw2 * res * c68) * sin(atArg * uFreq + tw);
  float negBase = -c73;
  float atArg2 = atArg * uAberrationFreq + tw;
  float py = p.y;
  float n80sq = n80 * n80;
  float bft = max(uBandFillThickness, 0.0001);
  float n139 = (c61 * res) * n78;
  float env68 = cw2 * c68;
  vec2 mouseUv = uMouse.xy / max(uResolution, vec2(1.0));
  float mouseLift = uMouse.z * 0.035 * exp(-pow((mouseUv.x * 2.0 - 1.0) * 2.4, 2.0));

  vec3 colAcc = vec3(0.0);
  vec3 highlightAcc = vec3(0.0);
  float baseWeightSum = 0.0;
  float highlightWeightSum = 0.0;
  float waveCount = clamp(floor(uWaveCount + 0.5), 1.0, 8.0);
  for (int i = 0; i < 8; i += 1) {
    float fi = float(i);
    if (fi >= waveCount) continue;
    float t13 = (waveCount > 1.0) ? fi / (waveCount - 1.0) : 0.5;
    // Geometry and opacity may resolve from 0 to 1, but the palette must not.
    // Mixing from white here caused the visible pastel flash after an answer.
    vec3 hue = wavePaletteColor(i);
    vec3 highlightHue = waveHighlightColor(i);
    baseWeightSum += max(max(hue.r, hue.g), hue.b);
    highlightWeightSum += max(max(highlightHue.r, highlightHue.g), highlightHue.b);
    float ph = atArg2 + mix(negBase, c73, t13);
    float w2 = env68 * sin(ph) + mouseLift;
    float dist = mix(edge, abs(py - w2), res);
    float rad = sqrt(dist * dist + n80sq) + n77;
    float k = dist * 0.02;
    float soft = mix(1.0 / (k * k + 1.0), 1.0, res);
    float glowL = (soft * n78) / rad;
    float band = max(0.0, max(py - max(waveBase, w2), min(waveBase, w2) - py));
    float fill = n139 / (band + bft);
    float layerEnergy = n81 * (fill + glowL);
    colAcc += hue * layerEnergy;
    highlightAcc += highlightHue * layerEnergy;
  }
  vec3 col = colAcc / max(baseWeightSum, 0.0001);
  vec3 highlightCol = highlightAcc / max(highlightWeightSum, 0.0001);
  float tail = omr * (c76 + 4.0);
  float dC = mix(edge, abs(py - waveBase), res);
  float radC = dC + n77;
  float kC = dC * 0.02;
  float softC = mix(1.0 / (kC * kC + 1.0), 1.0, res);
  float cg = (n78 * 0.5 * (softC + tail)) / radC;
  vec3 cgl = pow(vec3(cg) + col, vec3(1.5));
  float glowEnergy = max(max(cgl.r, cgl.g), cgl.b);
  float iceWhiteMix = smoothstep(0.35, 0.95, saturate(glowEnergy));
  float paletteEnergy = max(max(col.r, col.g), col.b);
  vec3 paletteColor = col / max(paletteEnergy, 0.0001);
  paletteColor = mix(uWaveColor0, paletteColor, step(0.0001, paletteEnergy));
  float highlightEnergy = max(max(highlightCol.r, highlightCol.g), highlightCol.b);
  vec3 highlightColor = highlightCol / max(highlightEnergy, 0.0001);
  highlightColor = mix(
    uWaveHighlightColor0,
    highlightColor,
    step(0.0001, highlightEnergy)
  );
  cgl = mix(paletteColor, highlightColor, iceWhiteMix) * glowEnergy;
  float ndcY = gid.y * 2.0 / uResolution.y - 1.0;
  float emC = max(clamp(uEdgeMask, 0.0, 1.0), 0.0001);
  float emMask = clamp((abs(ndcY) - 1.0 + clamp(uEdgeMaskInset, 0.0, 1.0)) / (-emC), 0.0, 1.0);
  emMask = smoothUnit(emMask);
  float fall = exp(-pow(px * uFalloff, 2.0));
  col = cgl * mix(1.0, emMask * fall, res) * res * saturate(uLayerOpacity);
  float m = max(max(col.r, col.g), col.b);
  vec3 huePreserved = col * ((m > 1.0) ? (1.0 / m) : 1.0);
  col = mix(huePreserved, min(col, vec3(1.0)), clamp(uWhiteClip, 0.0, 1.0));
  float alpha = saturate(max(max(col.r, col.g), col.b) * 1.15);
  outColor = vec4(col, alpha);
}
`,dt=`#version 300 es
precision highp float;
uniform vec2 uResolution;
uniform float uTime;
uniform vec4 uMouse;
uniform float uDotsResolved;
uniform float uEffectScale;
uniform vec2 uAnchor;
uniform float uRotation;
uniform float uRingRadius;
uniform float uDotRadius;
uniform float uPairOffset;
uniform float uPairSmoothness;
uniform float uSmoothness;
uniform float uProgress0;
uniform float uProgress1;
uniform float uProgress2;
uniform float uProgress3;
uniform float uProgress4;
uniform float uProgress5;
uniform float uScaleDuration;
uniform float uScaleStagger;
uniform float uScaleMin;
uniform float uScaleMax;
uniform float uGlowIntensity;
uniform float uFalloffPower;
uniform float uGlowFadeStart;
uniform float uGlowFadeEnd;
uniform float uDotsAberration;
uniform float uCenterCore;
uniform float uDotsScale;
uniform float uAppear;
uniform float uGather;
uniform float uCharge;
uniform float uFlash;
out vec4 outColor;

float saturate(float value) { return clamp(value, 0.0, 1.0); }
vec3 spectrumTri(float t) {
  return clamp(vec3(abs(t - 3.0) - 1.0, 2.0 - abs(t - 2.0), 2.0 - abs(t - 4.0)), 0.0, 1.0);
}
float progressAt(int index) {
  if (index == 0) return uProgress0;
  if (index == 1) return uProgress1;
  if (index == 2) return uProgress2;
  if (index == 3) return uProgress3;
  if (index == 4) return uProgress4;
  return uProgress5;
}
float dotsField(
  vec2 P, vec2 aberOff, vec2 centersA[6], vec2 centersB[6],
  vec2 dirs[6], float radii[6], bool psOn, bool smOn,
  float pairSmooth, float smoothness, float pairK, float smK
) {
  float field = 1.0e9;
  for (int j = 0; j < 6; j += 1) {
    vec2 ofs = aberOff * dirs[j];
    float lenA = length(P + ofs - centersA[j]);
    float lenB = length(P + ofs - centersB[j]);
    float dA = lenA - radii[j];
    float dB = lenB - radii[j];
    float dPair = min(dA, dB);
    if (psOn) {
      float h = max(pairSmooth - abs(lenA - lenB), 0.0) / pairSmooth;
      dPair = min(dA, dB) - h * h * pairK;
    }
    if (smOn) {
      float h2 = max(smoothness - abs(field - dPair), 0.0) / smoothness;
      field = min(field, dPair) - h2 * h2 * smK;
    } else {
      field = min(field, dPair);
    }
  }
  return field;
}

void main() {
  vec2 gid = vec2(gl_FragCoord.x, uResolution.y - gl_FragCoord.y);
  float mn = min(uResolution.x, uResolution.y);
  float halfMn = mn * 0.5;
  vec2 anchorC = uAnchor - 0.5;
  float aspect2 = uResolution.x / halfMn;
  vec2 anchorShift = vec2(aspect2, 2.0) * anchorC;
  float pr = max(uDotsScale * uEffectScale, 0.001);
  float drive = mod(uTime, 62.831848) * uRotation;
  float scaleDur = max(uScaleDuration, 0.001);
  float appear = saturate(uAppear) * saturate(uDotsResolved);
  float ringAmp = appear * uRingRadius;
  float pairAmp = appear * uPairOffset;
  vec2 centersA[6];
  vec2 centersB[6];
  vec2 dirs[6];
  float radii[6];

  for (int i = 0; i < 6; i += 1) {
    float fi = float(i);
    float angle = fi * 1.0471976 + drive;
    float ca = cos(angle);
    float sa = sin(angle);
    vec2 perp = vec2(-sa, ca);
    float fr = fract((fi * uScaleStagger + uTime) / scaleDur);
    float tri = 1.0 - abs(fr * 2.0 - 1.0);
    float x = saturate(tri);
    for (int k = 0; k < 8; k += 1) {
      float omx = 1.0 - x;
      float a3 = omx * 3.0;
      float c126 = (omx * 0.42) * a3;
      float x2 = x * x;
      float deriv = (x2 * 1.26) + (x * 0.96) * omx + c126;
      if (abs(deriv) < 0.000001) break;
      float num = ((x2 * 0.58) * a3 - tri) + (c126 + x2) * x;
      x = saturate(x - num / deriv);
    }
    float ss = x * x * (3.0 - 2.0 * x);
    float amp = mix(uScaleMin, uScaleMax, ss);
    amp = mix(amp, 0.45, uGather) * (1.0 - 0.18 * saturate(uCharge));
    vec2 dir = vec2(ca, sa);
    vec2 base = (ringAmp * dir) * (1.0 - 2.0 * progressAt(i));
    base = mix(base, dir * 0.008, uGather);
    float ph2 = pairAmp * amp * (1.0 - uGather);
    centersA[i] = base - ph2 * perp;
    centersB[i] = base + ph2 * perp;
    dirs[i] = dir;
    radii[i] = amp * uDotRadius;
  }

  vec2 uvPix = (gid + 0.5 - 0.5 * uResolution) / halfMn;
  vec2 P = (uvPix - anchorShift) / pr;
  bool psOn = uPairSmoothness > 0.0001;
  bool smOn = uSmoothness > 0.0001;
  float fadeRange = max(uGlowFadeEnd - uGlowFadeStart, 0.0001);
  float aberStep = uDotsAberration * 0.0909090936;
  vec3 colAcc = vec3(0.0);
  vec3 wSum = vec3(0.0);
  for (int i = 0; i < 12; i += 1) {
    float ti = float(i) * 0.363636374;
    vec3 hue = spectrumTri(ti);
    vec2 aberOff = vec2(-(aberStep * float(i)));
    float field = dotsField(P, aberOff, centersA, centersB, dirs, radii,
      psOn, smOn, uPairSmoothness, uSmoothness,
      uPairSmoothness * 0.25, uSmoothness * 0.25);
    float fm = max(field, 0.0);
    float glow = saturate(uGlowIntensity / pow(fm + 0.0001, uFalloffPower));
    float fadeT = clamp((fm - uGlowFadeStart) / fadeRange, 0.0, 1.0);
    float fade = 1.0 - fadeT * fadeT * (3.0 - 2.0 * fadeT);
    colAcc += hue * (fade * glow);
    wSum += hue;
  }
  float cfield = dotsField(P, vec2(0.0), centersA, centersB, dirs, radii,
    psOn, smOn, uPairSmoothness, uSmoothness,
    uPairSmoothness * 0.25, uSmoothness * 0.25);
  vec3 col = colAcc / max(wSum, vec3(0.0001));
  float cfm = max(cfield, 0.0);
  float cglow = saturate(uGlowIntensity / pow(cfm + 0.0001, uFalloffPower));
  float cfadeT = clamp((cfm - uGlowFadeStart) / fadeRange, 0.0, 1.0);
  float cfade = 1.0 - cfadeT * cfadeT * (3.0 - 2.0 * cfadeT);
  vec2 mouseUv = uMouse.xy / max(uResolution, vec2(1.0));
  float mouseBoost = 1.0 + uMouse.z * 0.35 + uMouse.w * 0.2 +
    smoothstep(0.0, 0.16, 1.0 - distance(mouseUv, vec2(0.5))) * 0.05;
  col = (col + (cglow * uCenterCore) * cfade) * appear * mouseBoost;
  col *= mix(1.0, 0.85, saturate(uGather)) *
    (1.0 + 0.35 * saturate(uCharge) + 1.2 * uFlash);
  float m = max(max(col.r, col.g), col.b);
  col *= (m > 1.0) ? (1.0 / m) : 1.0;
  float alpha = saturate(max(max(col.r, col.g), col.b));
  outColor = vec4(col, alpha);
}
`,ft=`#version 300 es
precision highp float;
uniform vec2 uResolution;
uniform sampler2D uBackground;
uniform vec2 uTextureSize;
uniform vec2 uCanvasSize;
uniform float uBackgroundReady;
out vec4 outColor;
vec2 coverUv(vec2 canvasUv) {
  vec2 pixel = canvasUv * uCanvasSize;
  float cover = max(uCanvasSize.x / uTextureSize.x, uCanvasSize.y / uTextureSize.y);
  vec2 fitted = uTextureSize * cover;
  vec2 offset = (fitted - uCanvasSize) * 0.5;
  return clamp((pixel + offset) / fitted, vec2(0.0), vec2(1.0));
}
vec3 fallbackBackground(vec2 uv) {
  float vignette = 1.0 - smoothstep(0.12, 0.95, distance(uv, vec2(0.5)));
  vec3 top = vec3(0.015, 0.018, 0.022);
  vec3 bottom = vec3(0.0, 0.0, 0.0);
  vec3 tint = mix(bottom, top, 1.0 - uv.y);
  return tint + vec3(0.02, 0.035, 0.055) * vignette;
}
void main() {
  vec2 pixel = vec2(gl_FragCoord.x, uResolution.y - gl_FragCoord.y);
  vec2 uv = pixel / uCanvasSize;
  vec3 image = texture(uBackground, coverUv(uv)).rgb;
  vec3 background = mix(fallbackBackground(uv), image, clamp(uBackgroundReady, 0.0, 1.0));
  outColor = vec4(background, 1.0);
}
`,pt=`#version 300 es
precision highp float;
uniform vec2 uResolution;
uniform sampler2D uEffectTexture;
uniform vec2 uCanvasSize;
uniform vec2 uEffectOrigin;
uniform vec2 uEffectSize;
uniform float uContainer;
uniform float uContainerBlack;
uniform float uContainerFade;
uniform float uContainerGauss;
uniform vec3 uContainerTint;
uniform float uAnger;
out vec4 outColor;
void main() {
  vec2 pixel = vec2(gl_FragCoord.x, uResolution.y - gl_FragCoord.y);
  vec2 effectUv = (pixel - uEffectOrigin) / uEffectSize;
  vec2 inRect = step(vec2(0.0), effectUv) * step(effectUv, vec2(1.0));
  if (inRect.x * inRect.y < 0.5) discard;
  vec4 effect = texture(uEffectTexture, vec2(effectUv.x, 1.0 - effectUv.y));
  float gy = clamp(effectUv.y, 0.0, 1.0);
  float t = clamp((gy - uContainerBlack) / max(uContainerFade, 0.001), 0.0, 1.0);
  float vfade = (gy <= uContainerBlack) ? 1.0 : exp(-uContainerGauss * t * t);
  float edgeLR = smoothstep(0.0, 0.14, min(effectUv.x, 1.0 - effectUv.x));
  float containerA = clamp(uContainer, 0.0, 1.0) * vfade * edgeLR;
  vec3 containerColor = mix(vec3(0.0), uContainerTint,
    clamp(uAnger, 0.0, 1.0) * vfade);
  float invEffectA = 1.0 - effect.a;
  vec3 outRGB = effect.rgb + containerColor * containerA * invEffectA;
  float outA = effect.a + containerA * invEffectA;
  outColor = vec4(outRGB, outA);
}
`,mt=`#version 300 es
precision highp float;
uniform vec2 uResolution;
uniform sampler2D uSceneTexture;
uniform sampler2D uBackground;
uniform vec2 uTextureSize;
uniform vec2 uPanelSize;
uniform vec2 uCanvasSize;
uniform vec2 uPanelOrigin;
uniform float uMarginPx;
uniform float uCornerRadius;
uniform float uHeight;
uniform float uCurvature;
uniform float uRefractAmount;
uniform float uAngle;
uniform float uGradRadialMix;
uniform float uKeyAngle;
uniform float uFillAngle;
uniform float uHlHeight;
uniform float uHlCut;
uniform float uHlNorm;
uniform float uHlAmount;
uniform float uHlCurv;
uniform float uBackgroundReady;
uniform float uTransparentOutside;
uniform vec4 uChip0;
uniform vec4 uChip1;
uniform vec4 uChip2;
uniform vec3 uChipState;
uniform vec3 uChipHover;
uniform float uChipRefract;
uniform float uChipHeight;
uniform float uChipHlAmount;
uniform float uChipFace;
out vec4 outColor;

float saturate(float x) { return clamp(x, 0.0, 1.0); }
vec2 rotate2d(vec2 v, float a) {
  float c = cos(a);
  float s = sin(a);
  return vec2(v.x * c - v.y * s, v.x * s + v.y * c);
}
vec2 coverUv(vec2 canvasUv) {
  vec2 pixel = canvasUv * uCanvasSize;
  float cover = max(uCanvasSize.x / uTextureSize.x, uCanvasSize.y / uTextureSize.y);
  vec2 fitted = uTextureSize * cover;
  vec2 offset = (fitted - uCanvasSize) * 0.5;
  return clamp((pixel + offset) / fitted, vec2(0.0), vec2(1.0));
}
vec3 fallbackBackground(vec2 uv) {
  float vignette = 1.0 - smoothstep(0.12, 0.95, distance(uv, vec2(0.5)));
  vec3 top = vec3(0.015, 0.018, 0.022);
  vec3 bottom = vec3(0.0, 0.0, 0.0);
  return mix(bottom, top, 1.0 - uv.y) + vec3(0.02, 0.035, 0.055) * vignette;
}
vec3 sampleBackground(vec2 canvasUv) {
  vec3 image = texture(uBackground, coverUv(canvasUv)).rgb;
  return mix(fallbackBackground(canvasUv), image, clamp(uBackgroundReady, 0.0, 1.0));
}
vec3 sampleScene(vec2 canvasUv) {
  return texture(uSceneTexture, vec2(canvasUv.x, 1.0 - canvasUv.y)).rgb;
}
float supercircleDistance(vec2 p, vec2 b, float n, vec2 param) {
  const float c = 1.528665;
  float an = abs(n);
  float ac = an * c;
  float m10 = mix(ac, an, max(param.x, param.y));
  vec2 v14 = (p - b) + vec2(m10);
  vec2 q = abs(max(vec2(0.0), (p - b) / max(ac, 0.0001) + vec2(1.0)));
  float l = length(q);
  float qmax = max(q.x, q.y);
  float qmin = min(q.x, q.y);
  float ratio = (qmax == 0.0) ? 0.0 : saturate(qmin / qmax);
  float poly = ((((-0.926054 * ratio + 3.15601) * ratio - 3.64122) * ratio + 1.26803) * ratio + 0.268531);
  float dCorner = (l + 1.0) - 1.0 / (1.0 - ratio * ratio * saturate(l) * poly);
  float dFar = length(max(vec2(0.0), q * c - vec2(0.528665))) * 0.654166 + 0.345834;
  float d57 = mix(dCorner, dFar, param.x);
  float d58 = mix(dCorner, dFar, param.y);
  float s = (q.y > q.x) ? 1.0 : -1.0;
  float t65 = saturate((0.5 - s) + s * ratio);
  float dist = mix(d57, d58, t65) - 1.0;
  float emin = min(max(v14.x, v14.y), 0.0);
  return emin + ac * dist;
}
vec2 cornerParam(vec2 halfSize, float r) {
  if (r < 0.0001) return vec2(0.0);
  return clamp((vec2(1.528665) - halfSize / r) / 0.528665, vec2(0.0), vec2(1.0));
}
float shapeDistance(vec2 p, vec2 halfSize, float cornerRadius) {
  float r = min(cornerRadius, min(halfSize.x, halfSize.y));
  if (r < 0.5) {
    vec2 dd = abs(p) - halfSize;
    return length(max(dd, vec2(0.0))) + min(max(dd.x, dd.y), 0.0);
  }
  return supercircleDistance(abs(p), halfSize, r, cornerParam(halfSize, r));
}
vec2 shapeGradient(vec2 p, vec2 halfSize, float cornerRadius, float radialMix) {
  float r = min(cornerRadius, min(halfSize.x, halfSize.y));
  vec2 param = cornerParam(halfSize, r);
  float ac = mix(r * 1.528665, r, max(param.x, param.y));
  vec2 pf = abs(p);
  vec2 v = max(vec2(0.0), (pf - halfSize) + vec2(ac));
  vec2 g = (v.x + v.y > 0.00001)
    ? normalize(v)
    : ((pf.x - halfSize.x > pf.y - halfSize.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0));
  vec2 cornerGrad = g * sign(p);
  vec2 centerRadial = normalize(vec2(p.x, halfSize.x * p.y / max(halfSize.y, 0.001)) + vec2(0.00001));
  return normalize(mix(cornerGrad, centerRadial, radialMix));
}
float refractionProfile(float t, float curvature) {
  float flatProfile = 1.0 - 0.2929 * (t < 1.0 ? 1.0 : 0.0);
  float circular = sqrt(max(1.0 - (1.0 - t) * (1.0 - t), 0.0));
  return mix(flatProfile, circular, curvature);
}
vec2 refractedUv(vec2 baseUv, float d, vec2 grad) {
  float t = clamp(-d / max(uHeight, 0.001), 0.0, 1.0);
  float mag = 1.0 - refractionProfile(t, uCurvature);
  vec2 dir = rotate2d(grad, uAngle);
  return baseUv + (uRefractAmount * mag * dir) / uCanvasSize;
}
float highlightLobe(float dist, float aa, vec2 n, float h, vec2 dir, float cut, float curv) {
  if (dist < -5.0) return 0.0;
  float t = saturate(dist / max(h, 0.001));
  float profile = mix(t < 1.0 ? 1.0 : 0.0, 1.0 - t, curv);
  float band = saturate(dist / aa + 0.5) * saturate((h - dist) / aa + 0.5) * profile;
  float angular = saturate((dot(dir, n) - cut) / max(1.0 - cut, 0.001));
  return band * angular;
}
float highlightBand(float d, vec2 grad, float aa) {
  float glen = max(length(grad), 0.0001);
  float dist = -d / glen;
  vec2 n = grad / glen;
  aa = max(aa, 0.0001);
  vec2 kdir = vec2(cos(uKeyAngle), sin(uKeyAngle));
  vec2 fdir = vec2(cos(uFillAngle), sin(uFillAngle));
  float key = highlightLobe(dist, aa, n, uHlHeight, kdir, uHlCut, uHlCurv);
  float fill = highlightLobe(dist, aa, n, uHlHeight, fdir, uHlCut, uHlCurv);
  float keyN = key / (1.0 + (1.0 - key) * uHlNorm);
  float fillN = fill / (1.0 + (1.0 - fill) * uHlNorm);
  return keyN + fillN;
}
vec4 glassFragment(vec2 pixel) {
  vec2 panelUv = (pixel - uPanelOrigin) / uPanelSize;
  vec2 inQuad = step(vec2(0.0), panelUv) * step(panelUv, vec2(1.0));
  vec2 halfSize = uPanelSize * 0.5 - vec2(uMarginPx);
  vec2 p = (panelUv - vec2(0.5)) * uPanelSize;
  float d = shapeDistance(p, halfSize, uCornerRadius);
  vec2 grad = shapeGradient(p, halfSize, uCornerRadius, uGradRadialMix);
  float panelHighlightDist = -d / max(length(grad), 0.0001);
  float panelHighlightAa = max(fwidth(panelHighlightDist), 0.0001);
  vec4 chips[3] = vec4[3](uChip0, uChip1, uChip2);
  vec2 chipP[3];
  float chipD[3];
  vec2 chipGrad[3];
  float chipHighlightAa[3];
  for (int i = 0; i < 3; i++) {
    vec4 chip = chips[i];
    chipP[i] = p - chip.xy;
    float cr = min(chip.z, chip.w);
    chipD[i] = shapeDistance(chipP[i], chip.zw, cr);
    chipGrad[i] = shapeGradient(chipP[i], chip.zw, cr, 0.35);
    float chipHighlightDist = -chipD[i] / max(length(chipGrad[i]), 0.0001);
    chipHighlightAa[i] = max(fwidth(chipHighlightDist), 0.0001);
  }
  // All derivatives run before fragment-varying exits, as required by GLSL ES.
  if (inQuad.x * inQuad.y < 0.5) return vec4(0.0);
  float alpha = 1.0 - smoothstep(-1.0, 1.0, d);
  if (alpha <= 0.001) return vec4(0.0);
  vec2 baseUv = (uPanelOrigin + panelUv * uPanelSize) / uCanvasSize;
  vec2 rUv = clamp(refractedUv(baseUv, d, grad), vec2(0.0), vec2(1.0));
  vec3 col = sampleScene(rUv);
  col += vec3(highlightBand(d, grad, panelHighlightAa) * uHlAmount);
  for (int i = 0; i < 3; i++) {
    float on = uChipState[i];
    vec4 chip = chips[i];
    if (on <= 0.001 || chip.z <= 0.5) continue;
    vec2 cp = chipP[i];
    float cd = chipD[i];
    float ca = (1.0 - smoothstep(-1.0, 1.0, cd)) * on;
    if (ca <= 0.001) continue;
    vec2 cgrad = chipGrad[i];
    float t = clamp(-cd / max(uChipHeight, 0.001), 0.0, 1.0);
    float mag = 1.0 - refractionProfile(t, 1.0);
    vec2 cUv = clamp(rUv + (uChipRefract * mag * cgrad) / uCanvasSize, vec2(0.0), vec2(1.0));
    float hov = uChipHover[i];
    vec3 chipCol = sampleScene(cUv);
    chipCol = mix(chipCol, vec3(1.0), uChipFace * (1.0 + 1.5 * hov));
    chipCol += vec3(highlightBand(cd, cgrad, chipHighlightAa[i]) * uChipHlAmount);
    col = mix(col, chipCol, ca);
  }
  return vec4(col, alpha);
}
void main() {
  vec2 pixel = vec2(gl_FragCoord.x, uResolution.y - gl_FragCoord.y);
  vec2 canvasUv = pixel / uCanvasSize;
  vec3 background = sampleBackground(canvasUv);
  vec4 glass = glassFragment(pixel);
  float a = saturate(glass.a);
  vec3 glassRgb = clamp(glass.rgb, 0.0, 1.25);
  vec4 framed = vec4(mix(background, glassRgb, a), 1.0);
  vec4 floating = vec4(glassRgb * a, a);
  outColor = mix(framed, floating, clamp(uTransparentOutside, 0.0, 1.0));
}
`,ht={
  baseColors:[[0,1,0],[0,123/255,1],[1,1,1]],highlightColors:[[0,1,0],[0,123/255,1],[1,1,1]],waveCount:3,audioBrightnessResponse:1,audioBrightnessSmoothing:!0
},gt={
  audioScale:1,uWhiteClip:1,uUnresolvedScale:.14,uAmplitude:.22,uFreq:1.1,uAberrationFreq:1,uWaveSpeed:-1,uWaveScale:.9,uAberration:2.6,uThickness:3,uIntensity:2,uFalloff:1.7,uEdgeMask:.4,uEdgeMaskInset:0,uBandFill:3e4,uBandFillThickness:.08,uSoftness:2.5,uLowAmplitude:6,uLowIntensity:1.5,uMidAberration:.8,uMidAberrationAmplitude:.05,uMidBandFill:0,uMidSoftness:.4,uHighAberration:.5,uHighAberrationAmplitude:.06
};
function _t(e,t,n=ht,r=t.low){
  let{
    audioScale:i,...a
  }
  =gt,o=Math.max(1,Math.min(8,Math.round(n.waveCount)));
  return[{
    name:`uResolved`,value:e.waveResolved
  },{
    name:`uLayerOpacity`,value:e.waveLayerOpacity
  },{
    name:`uEffectScale`,value:e.effectScale
  },{
    name:`uAnchor`,type:`vec2`,value:[.5,.5]
  },{
    name:`uWavePhase`,value:e.wavePhase
  },{
    name:`uLow`,value:t.low*i
  },{
    name:`uMid`,value:t.mid*i
  },{
    name:`uHigh`,value:t.high*i
  },{
    name:`uBrightnessLow`,value:Math.max(0,Math.min(1,r))
  },...n.baseColors.map((e,t)=>({
    name:`uWaveColor${t}`,type:`vec3`,value:e
  })),...n.highlightColors.map((e,t)=>({
    name:`uWaveHighlightColor${t}`,type:`vec3`,value:e
  })),{
    name:`uWaveCount`,value:o
  },{
    name:`uAudioBrightnessResponse`,value:Math.max(0,Math.min(1,n.audioBrightnessResponse))
  },...Object.entries(a).map(([e,t])=>({
    name:e,value:t
  }))]
}
function vt(e,t){
  return[{
    name:`uDotsResolved`,value:e.dotsResolved
  },{
    name:`uEffectScale`,value:e.effectScale
  },{
    name:`uAnchor`,type:`vec2`,value:[.5,.5]
  },{
    name:`uRotation`,value:1.4
  },{
    name:`uRingRadius`,value:.45
  },{
    name:`uDotRadius`,value:.1
  },{
    name:`uPairOffset`,value:.085
  },{
    name:`uPairSmoothness`,value:.2
  },{
    name:`uSmoothness`,value:.2
  },...t.map((e,t)=>({
    name:`uProgress${t}`,value:e.value
  })),{
    name:`uScaleDuration`,value:2
  },{
    name:`uScaleStagger`,value:.167
  },{
    name:`uScaleMin`,value:.001
  },{
    name:`uScaleMax`,value:.65
  },{
    name:`uGlowIntensity`,value:.04
  },{
    name:`uFalloffPower`,value:.7
  },{
    name:`uGlowFadeStart`,value:0
  },{
    name:`uGlowFadeEnd`,value:.7
  },{
    name:`uDotsAberration`,value:-.05
  },{
    name:`uCenterCore`,value:.5
  },{
    name:`uDotsScale`,value:1
  },{
    name:`uAppear`,value:e.dotsAppear
  },{
    name:`uGather`,value:e.gather
  },{
    name:`uCharge`,value:e.charge
  },{
    name:`uFlash`,value:e.flash
  }]
}
var yt=2,bt=20,xt=.7,St=1.18,Ct=44,wt=new Uint8Array([3,4,8,255]),Tt=.05,Et=.05,Dt={
  refractAmount:-32,highlightHeight:2.2,highlightCut:.52,highlightAmount:.72
};
function Ot(e,t,n,r){
  let i=Math.min(e,t)*.5,a=Math.max(0,Math.min(1,n)),o=i+(Ct*r-i)*a;
  return Math.min(i,o)
}
function kt(e){
  return typeof e==`number`||typeof e==`boolean`?[Number(e)]:Array.isArray(e)?e.flat(1/0).map(Number):ArrayBuffer.isView(e)&&!(e instanceof DataView)?Array.from(e,Number):[]
}
function At(e,t){
  if(e)return e;
  if(typeof t==`boolean`)return`bool`;
  if(typeof t==`number`)return`float`;
  let n=kt(t);
  return n.length===2?`vec2`:n.length===3?`vec3`:n.length===4?`vec4`:n.length===9?`mat3`:n.length===16?`mat4`:`float`
}
function jt(e,t){
  for(let n=0;n<e.length;n+=1)if(e[n]!==t[n])return!1;
  return!0
}
function Mt(e,t,n,r){
  let i=e.createShader(t);
  if(!i)throw Error(`Unable to create ${r} shader.`);
  if(e.shaderSource(i,n),e.compileShader(i),!e.getShaderParameter(i,e.COMPILE_STATUS)){
    let t=e.getShaderInfoLog(i)||`Unknown ${r} shader error.`;
    throw e.deleteShader(i),Error(t)
  }
  return i
}
function Nt(e,t,n){
  let r=Mt(e,e.VERTEX_SHADER,lt,`${n} vertex`),i=Mt(e,e.FRAGMENT_SHADER,t,`${n} fragment`),a=e.createProgram();
  if(!a)throw Error(`Unable to create ${n} program.`);
  if(e.attachShader(a,r),e.attachShader(a,i),e.linkProgram(a),e.deleteShader(r),e.deleteShader(i),!e.getProgramParameter(a,e.LINK_STATUS)){
    let t=e.getProgramInfoLog(a)||`Unknown ${n} link error.`;
    throw e.deleteProgram(a),Error(t)
  }
  return{
    label:n,program:a,uniforms:new Map,types:new Map,values:new Map
  }
}
function Pt(e){
  let t=e.createTexture();
  return e.bindTexture(e.TEXTURE_2D,t),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_S,e.CLAMP_TO_EDGE),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_WRAP_T,e.CLAMP_TO_EDGE),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MIN_FILTER,e.LINEAR),e.texParameteri(e.TEXTURE_2D,e.TEXTURE_MAG_FILTER,e.LINEAR),t
}
function Ft(e,t,n){
  let r=Pt(e);
  e.texImage2D(e.TEXTURE_2D,0,e.RGBA8,t,n,0,e.RGBA,e.UNSIGNED_BYTE,null);
  let i=e.createFramebuffer();
  if(e.bindFramebuffer(e.FRAMEBUFFER,i),e.framebufferTexture2D(e.FRAMEBUFFER,e.COLOR_ATTACHMENT0,e.TEXTURE_2D,r,0),e.checkFramebufferStatus(e.FRAMEBUFFER)!==e.FRAMEBUFFER_COMPLETE)throw e.deleteFramebuffer(i),e.deleteTexture(r),Error(`Siri framebuffer is incomplete.`);
  return e.bindFramebuffer(e.FRAMEBUFFER,null),{
    framebuffer:i,texture:r,width:t,height:n
  }
}
function It(e,t){
  t&&(e.deleteFramebuffer(t.framebuffer),e.deleteTexture(t.texture))
}
var Lt=class{
  canvas;
  gl;
  dpr=1;
  width=1;
  height=1;
  time=0;
  disposed=!1;
  error=null;
  panelOffset=[0,0];
  backgroundSize=[1,1];
  backgroundReady=0;
  backgroundTexture=null;
  effectTarget=null;
  sceneTarget=null;
  vertexArray=null;
  programs=null;
  contextLost=!1;
  lastImage=null;
  brightnessLow=0;
  onContextLost;
  onContextRestored;
  constructor(e){
    if(this.canvas=e,this.gl=e.getContext(`webgl2`,{
      alpha:!0,antialias:!1,depth:!1,stencil:!1,premultipliedAlpha:!0,preserveDrawingBuffer:!1
    }),this.onContextLost=e=>{
      e.preventDefault(),this.contextLost=!0,this.effectTarget=null,this.sceneTarget=null,this.setFallback(!0)
    },this.onContextRestored=()=>{
      try{
        this.contextLost=!1,this.error=null,this.initGL(),this.canvas.dispatchEvent(new CustomEvent(`siri-render-restored`))
      }catch(e){
        this.fail(e)
      }
    },!this.gl){
      this.fail(Error(`WebGL2 is unavailable.`));
      return
    }
    e.addEventListener(`webglcontextlost`,this.onContextLost),e.addEventListener(`webglcontextrestored`,this.onContextRestored);
    try{
      this.initGL()
    }catch(e){
      this.fail(e)
    }
  }
  initGL(){
    let e=this.gl;
    e&&(this.vertexArray=e.createVertexArray(),this.programs={
      wave:Nt(e,ut,`wave`),dots:Nt(e,dt,`dots`),background:Nt(e,ft,`background`),effectComposite:Nt(e,pt,`effect composite`),glassComposite:Nt(e,mt,`glass composite`)
    },this.backgroundTexture=Pt(e),e.pixelStorei(e.UNPACK_FLIP_Y_WEBGL,!1),e.texImage2D(e.TEXTURE_2D,0,e.RGBA,1,1,0,e.RGBA,e.UNSIGNED_BYTE,wt),e.bindVertexArray(this.vertexArray),e.disable(e.DEPTH_TEST),e.disable(e.STENCIL_TEST),this.backgroundReady=0,this.backgroundSize=[1,1],this.lastImage&&this.setBackgroundImage(this.lastImage))
  }
  setBackgroundImage(e){
    let t=this.gl;
    if(this.lastImage=e,!t||this.disposed||this.error||this.contextLost||!this.backgroundTexture)return;
    t.bindTexture(t.TEXTURE_2D,this.backgroundTexture),t.pixelStorei(t.UNPACK_FLIP_Y_WEBGL,!1),t.texImage2D(t.TEXTURE_2D,0,t.RGBA,t.RGBA,t.UNSIGNED_BYTE,e);
    let n=e;
    this.backgroundSize=[n.naturalWidth||n.width||1,n.naturalHeight||n.height||1],this.backgroundReady=1
  }
  render(e,t,n=0,r=Dt,i=ht){
    if(!(!this.gl||!this.programs||this.disposed||this.error||this.contextLost))try{
      this.time=(this.time+Math.max(0,Math.min(n,.1)))%1e5;
      let a=this.updateBrightnessLow(t.low,n,i.audioBrightnessSmoothing);
      this.resize();
      let o=this.layout(e.surface,e.sizes);
      this.ensureTargets(o),this.renderEffectPass(e.surface,e.progress,t,a,o,i),this.renderScenePass(o),this.renderGlassPass(o,r),this.canvas.dataset.fallback===`true`&&this.setFallback(!1)
    }catch(e){
      this.fail(e)
    }
  }
  dispose(){
    if(this.disposed)return;
    this.disposed=!0,this.canvas.removeEventListener(`webglcontextlost`,this.onContextLost),this.canvas.removeEventListener(`webglcontextrestored`,this.onContextRestored);
    let e=this.gl;
    if(e){
      It(e,this.effectTarget),It(e,this.sceneTarget),this.backgroundTexture&&e.deleteTexture(this.backgroundTexture);
      for(let t of Object.values(this.programs||{
      }))e.deleteProgram(t.program);
      this.vertexArray&&e.deleteVertexArray(this.vertexArray),this.effectTarget=null,this.sceneTarget=null,this.backgroundTexture=null
    }
  }
  resize(){
    let e=Math.max(1,this.canvas.clientWidth||window.innerWidth||1),t=Math.max(1,this.canvas.clientHeight||window.innerHeight||1),n=Math.min(yt,Math.max(1,window.devicePixelRatio||1)),r=Math.max(1,Math.round(e*n)),i=Math.max(1,Math.round(t*n));
    r===this.width&&i===this.height&&n===this.dpr||(this.dpr=n,this.width=r,this.height=i,this.canvas.width=r,this.canvas.height=i)
  }
  updateBrightnessLow(e,t,n){
    let r=Math.max(0,Math.min(1,e||0)),i=Math.max(0,Math.min(t,.1));
    if(!n||i===0)return this.brightnessLow=r,this.brightnessLow;
    let a=r>this.brightnessLow?Tt:Et,o=1-Math.exp(-i/a);
    return this.brightnessLow+=(r-this.brightnessLow)*o,this.brightnessLow
  }
  layout(e,t){
    let n=1+e.press*.018,r=e.answer||0,i=Math.max(0,Math.min(1,r)),a=xt+(1-xt)*i,o=bt*this.dpr*a,s=t.expanded.width*this.dpr,c=t.expanded.height*this.dpr,l=Math.max(0,Math.min(1,((e.dotsResolved??-1)+1)*.5)),u=s+(c-s)*l,d=Math.min(t.answer.width*this.dpr,this.width-48*this.dpr),f=t.answer.height*this.dpr,p=(u+(d-u)*r)*n,m=(c+(f-c)*r)*n,h=p+o*2,g=m+o*2,_=Math.max(1,Math.round(p*St)),v=Math.max(1,Math.round(m*St)),y=(this.width-h)*.5+this.panelOffset[0],b=(this.height-g)*.5+this.panelOffset[1],x=b+g*.5;
    return{
      effectWidth:_,effectHeight:v,effectOrigin:[(this.width-_)*.5+this.panelOffset[0],x-v*.5],effectSize:[_,v],panelOrigin:[y,b],panelSize:[h,g],margin:o,materialScale:a,cornerRadius:Ot(p,m,r,this.dpr),containerStrength:.9*Math.min(1,Math.max(0,Math.max(e.sharedResolved||0,r)))
    }
  }
  ensureTargets(e){
    let t=this.gl;
    (!this.effectTarget||this.effectTarget.width!==e.effectWidth||this.effectTarget.height!==e.effectHeight)&&(It(t,this.effectTarget),this.effectTarget=Ft(t,e.effectWidth,e.effectHeight)),(!this.sceneTarget||this.sceneTarget.width!==this.width||this.sceneTarget.height!==this.height)&&(It(t,this.sceneTarget),this.sceneTarget=Ft(t,this.width,this.height))
  }
  renderEffectPass(e,t,n,r,i,a){
    let o=this.gl;
    o.bindFramebuffer(o.FRAMEBUFFER,this.effectTarget.framebuffer),o.viewport(0,0,i.effectWidth,i.effectHeight),o.clearColor(0,0,0,0),o.clear(o.COLOR_BUFFER_BIT),o.enable(o.BLEND),o.blendEquation(o.FUNC_ADD),o.blendFunc(o.ONE,o.ONE_MINUS_SRC_ALPHA);
    let s=[{
      name:`uResolution`,type:`vec2`,value:[i.effectWidth,i.effectHeight]
    },{
      name:`uTime`,value:this.time
    },{
      name:`uMouse`,type:`vec4`,value:[i.effectWidth*.5,i.effectHeight*.5,e.press,0]
    }];
    this.draw(this.programs.wave,[...s,..._t(e,n,a,r)]),this.draw(this.programs.dots,[...s,...vt(e,t)]),o.disable(o.BLEND)
  }
  renderScenePass(e){
    let t=this.gl;
    t.bindFramebuffer(t.FRAMEBUFFER,this.sceneTarget.framebuffer),t.viewport(0,0,this.width,this.height),t.clearColor(0,0,0,1),t.clear(t.COLOR_BUFFER_BIT),this.draw(this.programs.background,[{
      name:`uResolution`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uTextureSize`,type:`vec2`,value:this.backgroundSize
    },{
      name:`uCanvasSize`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uBackgroundReady`,value:this.backgroundReady
    }],[{
      name:`uBackground`,texture:this.backgroundTexture,unit:0
    }]),t.enable(t.BLEND),t.blendEquation(t.FUNC_ADD),t.blendFunc(t.ONE,t.ONE_MINUS_SRC_ALPHA),this.draw(this.programs.effectComposite,[{
      name:`uResolution`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uCanvasSize`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uEffectOrigin`,type:`vec2`,value:e.effectOrigin
    },{
      name:`uEffectSize`,type:`vec2`,value:e.effectSize
    },{
      name:`uContainer`,value:e.containerStrength
    },{
      name:`uContainerBlack`,value:.25
    },{
      name:`uContainerFade`,value:1
    },{
      name:`uContainerGauss`,value:8
    },{
      name:`uContainerTint`,type:`vec3`,value:[.36,.04,.05]
    },{
      name:`uAnger`,value:0
    }],[{
      name:`uEffectTexture`,texture:this.effectTarget.texture,unit:0
    }]),t.disable(t.BLEND)
  }
  renderGlassPass(e,t){
    let n=this.gl;
    n.bindFramebuffer(n.FRAMEBUFFER,null),n.viewport(0,0,this.width,this.height),n.clearColor(0,0,0,0),n.clear(n.COLOR_BUFFER_BIT),this.draw(this.programs.glassComposite,[{
      name:`uResolution`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uTextureSize`,type:`vec2`,value:this.backgroundSize
    },{
      name:`uPanelSize`,type:`vec2`,value:e.panelSize
    },{
      name:`uCanvasSize`,type:`vec2`,value:[this.width,this.height]
    },{
      name:`uPanelOrigin`,type:`vec2`,value:e.panelOrigin
    },{
      name:`uMarginPx`,value:e.margin
    },{
      name:`uCornerRadius`,value:e.cornerRadius
    },{
      name:`uHeight`,value:18*this.dpr*e.materialScale
    },{
      name:`uCurvature`,value:1
    },{
      name:`uRefractAmount`,value:t.refractAmount*this.dpr*e.materialScale
    },{
      name:`uAngle`,value:0
    },{
      name:`uGradRadialMix`,value:.08
    },{
      name:`uKeyAngle`,value:Math.PI*.25
    },{
      name:`uFillAngle`,value:Math.PI*1.25
    },{
      name:`uHlHeight`,value:t.highlightHeight*this.dpr*e.materialScale
    },{
      name:`uHlCut`,value:t.highlightCut
    },{
      name:`uHlNorm`,value:8
    },{
      name:`uHlAmount`,value:t.highlightAmount
    },{
      name:`uHlCurv`,value:1
    },{
      name:`uBackgroundReady`,value:this.backgroundReady
    },{
      name:`uTransparentOutside`,value:1
    },{
      name:`uChip0`,type:`vec4`,value:[0,0,0,0]
    },{
      name:`uChip1`,type:`vec4`,value:[0,0,0,0]
    },{
      name:`uChip2`,type:`vec4`,value:[0,0,0,0]
    },{
      name:`uChipState`,type:`vec3`,value:[0,0,0]
    },{
      name:`uChipHover`,type:`vec3`,value:[0,0,0]
    },{
      name:`uChipRefract`,value:-22*this.dpr
    },{
      name:`uChipHeight`,value:7*this.dpr
    },{
      name:`uChipHlAmount`,value:.6
    },{
      name:`uChipFace`,value:.1
    }],[{
      name:`uSceneTexture`,texture:this.sceneTarget.texture,unit:0
    },{
      name:`uBackground`,texture:this.backgroundTexture,unit:1
    }])
  }
  draw(e,t=[],n=[]){
    let r=this.gl;
    r.useProgram(e.program),r.bindVertexArray(this.vertexArray);
    for(let t of n){
      let n=this.getUniformLocation(e,t.name);
      n!==null&&(r.activeTexture(r.TEXTURE0+t.unit),r.bindTexture(r.TEXTURE_2D,t.texture),r.uniform1i(n,t.unit))
    }
    for(let n of t)this.setUniform(e,n.name,n.value,n.type);
    r.drawArrays(r.TRIANGLES,0,3)
  }
  setUniform(e,t,n,r){
    let i=this.gl,a=this.getUniformLocation(e,t);
    if(a===null)return;
    let o=e.types.get(t);
    o===void 0&&(o=At(r,n),e.types.set(t,o));
    let s=kt(n),c=e.values.get(t);
    c!==void 0&&c.length===s.length&&jt(c,s)||(e.values.set(t,s),o===`int`||o===`sampler2D`||o===`bool`?i.uniform1i(a,s[0]||0):o===`vec2`?i.uniform2fv(a,s.slice(0,2)):o===`vec3`?i.uniform3fv(a,s.slice(0,3)):o===`vec4`?i.uniform4fv(a,s.slice(0,4)):o===`mat3`?i.uniformMatrix3fv(a,!1,s.slice(0,9)):o===`mat4`?i.uniformMatrix4fv(a,!1,s.slice(0,16)):i.uniform1f(a,s[0]||0))
  }
  getUniformLocation(e,t){
    if(e.uniforms.has(t))return e.uniforms.get(t);
    let n=this.gl.getUniformLocation(e.program,t);
    return e.uniforms.set(t,n),n
  }
  fail(e){
    this.error=e instanceof Error?e:Error(String(e)),this.setFallback(!0),this.canvas.dispatchEvent(new CustomEvent(`siri-render-error`,{
      detail:{
        message:this.error.message
      }
    }))
  }
  setFallback(e){
    e?this.canvas.dataset.fallback=`true`:delete this.canvas.dataset.fallback
  }
},Rt=Math.PI*2,zt=1,Bt=1e-4;
function Vt(e){
  let t=Math.max(e.response,Bt),n=Math.max(0,e.dampingRatio),r=Rt/t;
  return{
    mass:zt,stiffness:zt*r*r,damping:2*n*zt*r,naturalAngularFrequency:r
  }
}
function Ht(e){
  if(`stiffness`in e){
    let t=e.mass||zt;
    return{
      mass:t,stiffness:e.stiffness,damping:e.damping,naturalAngularFrequency:Math.sqrt(e.stiffness/t)
    }
  }
  return`duration`in e?Vt({
    response:e.duration,dampingRatio:Math.max(.05,1-Math.max(0,e.bounce))
  }):Vt(e)
}
function Ut(e,t,n,r,i){
  let a=r.stiffness/r.mass,o=r.naturalAngularFrequency,s=r.damping/(2*r.mass),c=Math.max(i,0),l=e-n;
  if(c<=0||l===0&&t===0)return[e,t];
  let u,d;
  if(s<o){
    let e=Math.sqrt(a-s*s),n=Math.exp(-s*c),r=Math.cos(e*c),i=Math.sin(e*c),o=l,f=(t+s*l)/e,p=o*r+f*i;
    u=n*p,d=n*(-s*p+(-o*e*i+f*e*r))
  }else if(o<s){
    let e=Math.sqrt(s*s-a),n=-s+e,r=-s-e,i=(t-r*l)/(n-r),o=l-i,f=Math.exp(n*c),p=Math.exp(r*c);
    u=i*f+o*p,d=i*n*f+o*r*p
  }else{
    let e=Math.exp(-s*c),n=t+s*l,r=l+n*c;
    u=e*r,d=e*(n-s*r)
  }
  return[n+u,d]
}
var Wt=class{
  value;
  velocity=0;
  target;
  parameters;
  constructor(e,t){
    this.value=e,this.target=e,this.parameters=Ht(t)
  }
  setOptions(e){
    this.parameters=Ht(e)
  }
  setTarget(e,t){
    t&&this.setOptions(t),this.target=e
  }
  jump(e){
    this.value=e,this.velocity=0,this.target=e
  }
  step(e){
    return[this.value,this.velocity]=Ut(this.value,this.velocity,this.target,this.parameters,e),this.value
  }
},Gt=128*.7,Kt=Math.round(Gt*.7),qt={
  response:.314,dampingRatio:1
},Jt={
  response:.3,dampingRatio:1
},Yt={
  response:.28,dampingRatio:1
},Xt={
  response:.314,dampingRatio:1
},Zt={
  duration:.9,bounce:.55
},Qt={
  response:.5,dampingRatio:1
},$t={
  duration:.55,bounce:.5
},en={
  response:.18,dampingRatio:1
},tn={
  response:.5,dampingRatio:.8
},nn=.2,rn=2.5,an=.6,on=.3,sn=7,cn=1/30,ln=62.831848,un=-2.5,dn=-12,fn=.4,pn={
  idle:{
    waveActive:!0,fluidDotsActive:!1
  },listening:{
    waveActive:!0,fluidDotsActive:!1
  },thinking:{
    waveActive:!1,fluidDotsActive:!0
  },answer:{
    waveActive:!1,fluidDotsActive:!1
  }
};
function mn(e){
  return{
    fluidDots:e.fluidDotsActive?1:-1,effectScale:e.fluidDotsActive?2/3:1
  }
}
function hn(e,t){
  let n=Math.min(Math.max(t,0),.1);
  for(;n>0;){
    let t=Math.min(n,cn);
    for(let n of[`fluidDots`,`effectScale`]){
      let r=(e.current[n]-e.target[n])*-400+e.velocity[n]*-40;
      e.velocity[n]+=r*t,e.current[n]+=e.velocity[n]*t
    }
    n-=t
  }
}
function gn(e){
  return Math.max(0,Math.min(1,Math.max(e.low||0,e.mid||0,e.high||0)*fn))
}
function _n(){
  let e=mn(pn.idle),t={
    waveOpacity:0,wavePhase:0,waveResolved:-1,sharedResolved:0,dotsAppear:0,dotsResolved:e.fluidDots,effectScale:e.effectScale,waveLayerOpacity:0,press:0,gather:0,charge:0,flash:0,answer:0
  },n={
    waveOpacity:new Wt(0,qt),dotsAppear:new Wt(0,Xt),press:new Wt(0,Yt)
  },r={
    current:{
      ...e
    },velocity:{
      fluidDots:0,effectScale:0
    },target:{
      ...e
    }
  },i=Array.from({
    length:6
  },()=>({
    value:0
  })),a=i.map(()=>new Wt(0,Zt)),o=new Wt(0,Qt),s=new Wt(0,en),c=new Wt(0,tn),l=`idle`,u=0,d=0,f=0,p=1/0,m=null,h=0,g=0;
  function _(){
    d=0,u=0,f=0,p=1/0;
    for(let e of a)e.setTarget(0,Zt)
  }
  function v(){
    m=null,h=0,g=0,o.jump(0),o.setOptions(Qt),s.jump(0)
  }
  return{
    sizes:{
      expanded:{
        width:Gt,height:Kt
      },answer:{
        width:460,height:150
      }
    },surface:t,progress:i,get state(){
      return l
    },select(e){
      let t=pn[e],i=mn(t),a=r.target.fluidDots!==i.fluidDots||r.target.effectScale!==i.effectScale;
      l=e,f=0,n.waveOpacity.setTarget(+!!t.waveActive,t.waveActive?qt:Jt),r.target=i,a&&(r.velocity={
        fluidDots:0,effectScale:0
      }),e!==`thinking`&&_(),(e===`listening`||e===`thinking`)&&v(),c.setTarget(+(e===`answer`),tn)
    },conclude(){
      return l!==`thinking`||m?0:(m=`gather`,h=0,f=0,o.setTarget(1,Qt),Math.round((an+on)*1e3))
    },setPressed(e){
      n.press.setTarget(+!!e,Yt)
    },tick(e,v){
      t.waveOpacity=n.waveOpacity.step(e),t.press=n.press.step(e),hn(r,e),t.dotsResolved=r.current.fluidDots,t.effectScale=r.current.effectScale,t.waveResolved=t.waveOpacity*2-1,t.sharedResolved=Math.max(t.waveResolved,t.dotsResolved,0),t.waveLayerOpacity=.98*Math.min(1,Math.max(0,t.waveOpacity));
      let y=un+dn*gn(v);
      t.wavePhase=(t.wavePhase+y*e)%ln,t.wavePhase<0&&(t.wavePhase+=ln),n.dotsAppear.setTarget(Math.max(t.dotsResolved,0),Xt),t.dotsAppear=n.dotsAppear.step(e),m&&(h+=e,m===`gather`&&h>=an?(m=`charge`,s.setTarget(1,en)):m===`charge`&&h>=an+on&&(m=null,g=1,o.setTarget(0,$t),s.jump(0))),t.gather=o.step(e),t.charge=s.step(e),t.answer=c.step(e),g*=Math.exp(-sn*e),g<.001&&(g=0),t.flash=g,l===`thinking`&&t.dotsResolved>0?(m||(f+=e),f>=rn&&(f=0,d=u,u=u>.5?0:1,p=0)):_(),p+=e;
      for(let t=0;t<a.length;t+=1){
        let n=t*nn>p?d:u;
        a[t].setTarget(n,Zt),i[t].value=a[t].step(e)
      }
    }
  }
}

var vn={
  low:0,mid:0,high:0
};
function yn(e){
  let t=Math.max(0,Math.min(1,e));
  return{
    low:t,mid:Math.min(1,t*.82),high:Math.min(1,t*.58)
  }
}

// ---------------------------------------------------------------------------------------------------
// CapsuleView: plain-JS replacement for Type's React component `bn`
//   bn({phase, audioLevel=0, timeScale=1, glassMaterial, waveAppearance})
// Mounting the view = Type rendering <bn/> (the dictation window shows it only while recording or
// processing); unmounting = Type removing it. The refs and effects map 1:1:
//   a.current canvas · o.current machine (_n) · s.current renderer (Lt) · c.current rAF id
//   l.current audio level · u.current time scale · d/f.current glass material / wave appearance
//   p.current prefers-reduced-motion · m.current retry count · h.current retry timer
//   g (state) renderer generation, bumped to rebuild after a WebGL failure · v (state) renderer unavailable
// ---------------------------------------------------------------------------------------------------
class CapsuleView {
  constructor(host, options = {}) {
    this.host = host;
    this.onLog = options.onLog || function () {};
    this.phase = "listening"; // "listening" | "transcribing"
    this.audioLevel = 0; // l.current
    this.timeScale = 1; // u.current
    this.glassMaterial = undefined; // d.current (undefined -> Lt.render default Dt)
    this.waveAppearance = undefined; // f.current (undefined -> Lt.render default ht)
    this.root = null;
    this.canvas = null; // a.current
    this.machine = null; // o.current
    this.renderer = null; // s.current
    this.raf = 0; // c.current
    this.reduced = false; // p.current
    this.retry = 0; // m.current
    this.retryTimer = null; // h.current
    this.unavailable = false; // v
    this.cleanup = null; // cleanup of the [g] effect
  }

  get mounted() {
    return this.root !== null;
  }

  // <bn phase={phase}/> appears. Calling it again while mounted only updates the phase.
  mount(phase) {
    if (this.root) {
      this.setPhase(phase);
      return;
    }
    this.phase = phase === "transcribing" ? "transcribing" : "listening";
    const root = document.createElement("div");
    root.className = "golos-dictation-capsule";
    const canvas = document.createElement("canvas");
    canvas.className = "golos-capsule-canvas";
    canvas.setAttribute("aria-hidden", "true");
    root.appendChild(canvas);
    this.host.appendChild(root);
    this.root = root;
    this.canvas = canvas;
    this.retry = 0;
    this.unavailable = false;
    this.applyAttributes();
    this.start();
  }

  // <bn/> disappears (React unmount): effect cleanup, then the DOM node goes away.
  unmount() {
    if (!this.root) return;
    this.stop();
    this.root.remove();
    this.root = null;
    this.canvas = null;
    this.retry = 0;
    this.unavailable = false;
  }

  // l.current=t
  setLevel(level) {
    const t = Number(level);
    this.audioLevel = Number.isFinite(t) ? t : 0;
  }

  // u.current=Math.max(.05,Math.min(4,n))
  setTimeScale(n) {
    this.timeScale = Math.max(.05, Math.min(4, n));
  }

  // effect [e] (phase)
  setPhase(phase) {
    const e = phase === "transcribing" ? "transcribing" : "listening";
    if (e === this.phase) return;
    this.phase = e;
    this.applyAttributes();
    let t = this.machine, n = this.renderer;
    if (!t || !n) return;
    let r = e === `transcribing` ? `thinking` : `listening`;
    if (t.state !== r && t.select(r), this.reduced) {
      for (let e = 0; e < 120; e += 1) t.tick(1 / 60, vn);
      n.render(t, vn, 0, this.glassMaterial, this.waveAppearance)
    }
  }

  // effect [r,i] (glassMaterial, waveAppearance)
  setAppearance(glassMaterial, waveAppearance) {
    this.glassMaterial = glassMaterial;
    this.waveAppearance = waveAppearance;
    let e = this.machine, t = this.renderer;
    !this.reduced || !e || !t || t.render(e, vn, 0, glassMaterial, waveAppearance)
  }

  // the JSX of bn: attributes of the wrapper div and the canvas
  applyAttributes() {
    if (!this.root) return;
    const v = this.unavailable;
    const visual = this.phase === `transcribing` ? `thinking` : `listening`;
    const root = this.root;
    if (v) root.setAttribute("data-renderer-unavailable", "true");
    else root.removeAttribute("data-renderer-unavailable");
    root.setAttribute("data-visual-state", visual);
    if (v) {
      root.removeAttribute("role");
      root.setAttribute("aria-hidden", "true");
      root.removeAttribute("aria-label");
    } else {
      root.setAttribute("role", "status");
      root.removeAttribute("aria-hidden");
      root.setAttribute("aria-label", this.phase === `transcribing` ? `Распознаём речь` : `Идёт запись`);
    }
    this.canvas.setAttribute("data-visual-state", visual);
  }

  setUnavailable(value) {
    if (this.unavailable === value) return;
    this.unavailable = value;
    this.applyAttributes();
  }

  // effect [g]: build machine + renderer, run the rAF loop
  start() {
    let t = this.canvas;
    if (!t) return;
    let n = _n(), r = new Lt(t), i = window.matchMedia(`(prefers-reduced-motion: reduce)`).matches;
    this.machine = n, this.renderer = r, this.reduced = i, n.select(this.phase === `transcribing` ? `thinking` : `listening`);
    let g = () => {
      for (let e = 0; e < 120; e += 1) n.tick(1 / 60, vn);
      r.render(n, vn, 0, this.glassMaterial, this.waveAppearance), r.error || this.setUnavailable(!1)
    }, v = performance.now(), b = e => {
      this.raf = 0;
      let t = Math.min(.1, Math.max(0, (e - v) / 1e3)) * this.timeScale;
      v = e;
      let i = n.state === `listening` ? yn(this.audioLevel) : vn;
      n.tick(t, i), r.render(n, i, t, this.glassMaterial, this.waveAppearance), r.error || (this.setUnavailable(!1), this.raf = requestAnimationFrame(b))
    }, x = () => {
      this.raf !== 0 || r.error || (v = performance.now(), this.raf = requestAnimationFrame(b))
    }, S = () => {
      this.retry = 0, this.setUnavailable(!1), i ? g() : x()
    }, C = e => {
      this.setUnavailable(!0);
      let t = e.detail?.message;
      this.onLog({
        level: `warn`, message: `Capsule WebGL renderer failed; hiding capsule`, meta: {
          message: t, retry: this.retry
        }, scope: `capsule`
      }), !(this.retry >= 2 || this.retryTimer !== null) && (this.retry += 1, this.retryTimer = window.setTimeout(() => {
        this.retryTimer = null, this.rebuild()
      }, 750 * this.retry))
    };
    t.addEventListener(`siri-render-restored`, S), t.addEventListener(`siri-render-error`, C), r.error && C(new CustomEvent(`siri-render-error`, {
      detail: {
        message: r.error.message
      }
    })), i ? g() : x();
    this.cleanup = () => {
      cancelAnimationFrame(this.raf), this.raf = 0, this.retryTimer !== null && (window.clearTimeout(this.retryTimer), this.retryTimer = null), t.removeEventListener(`siri-render-restored`, S), t.removeEventListener(`siri-render-error`, C), r.dispose(), this.machine = null, this.renderer = null
    }
  }

  stop() {
    const c = this.cleanup;
    this.cleanup = null;
    if (c) c();
  }

  // Type: `_(e=>e+1)` bumps g, React runs the [g] cleanup and the effect again on the same canvas
  rebuild() {
    if (!this.canvas) return;
    this.stop();
    this.start();
  }
}

window.TypeCapsule = { Lt, Wt, _n, yn, vn, Dt, ht, CapsuleView };
})();
