/* json_out.h — native 层内部使用的小型 JSON 写出辅助（仅头文件） */
#pragma once

#include <windows.h>
#include <cstdio>
#include <cstring>
#include <string>

namespace nj {

/* 宽字符串 -> UTF-8 */
inline std::string to_utf8(const wchar_t* w) {
    if (!w) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return "";
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, s.data(), n, nullptr, nullptr);
    if (!s.empty() && s.back() == '\0') s.pop_back();
    return s;
}

/* 追加一个 JSON 字符串字段值（含转义） */
inline void put_string(std::string& out, const std::wstring& w) {
    out += '"';
    std::string u = to_utf8(w.c_str());
    for (unsigned char ch : u) {
        switch (ch) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (ch < 0x20) {
                    char e[8];
                    snprintf(e, sizeof(e), "\\u%04x", ch);
                    out += e;
                } else {
                    out += static_cast<char>(ch);
                }
        }
    }
    out += '"';
}

inline void put_string(std::string& out, const wchar_t* w) {
    put_string(out, std::wstring(w ? w : L""));
}

/* 拷贝到调用方缓冲（保证 NUL 结尾） */
inline void copy_out(const std::string& s, char* buf, size_t len) {
    if (!buf || len == 0) return;
    size_t n = s.size();
    if (n > len - 1) n = len - 1;
    std::memcpy(buf, s.data(), n);
    buf[n] = '\0';
}

}  // namespace nj