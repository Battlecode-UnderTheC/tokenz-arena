// arena-metrics: compute() for many replays at once, on every core.
//
//   arena-metrics --describe                 the metrics, as JSON (what metrics.describe() lists)
//   arena-metrics REPLAY|-                   one replay's metrics (- reads it from stdin), JSON on stdout
//   arena-metrics --bench REPLAY...          where one thread's time goes
//   arena-metrics [-j N] < pairs             each stdin line "REPLAY<TAB>OUT.json": writes OUT.json
//                                            (atomically), then prints "ok REPLAY" or "error REPLAY: why"
// A replay that can't be read gets {"error": "native: ..."} in its OUT.json, as the Python backfill
// writes for one it can't read.
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "core.h"

static std::string slurp(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("can't read " + path);
  std::ostringstream s;
  s << f.rdbuf();
  return s.str();
}

static std::string escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    if ((unsigned char)c >= 0x20) o += c;
  }
  return o;
}

int main(int argc, char** argv) {
  std::vector<std::string> args(argv + 1, argv + argc);
  if (args.size() == 1 && args[0] == "--describe") {
    std::cout << am::describe_json() << "\n";
    return 0;
  }
  if (args.size() >= 2 && args[0] == "--bench") {        // where one thread's time goes
    std::vector<std::string> blobs;
    for (size_t i = 1; i < args.size(); i++) blobs.push_back(slurp(args[i]));
    for (auto& b : blobs) am::compute(b);
    const auto& p = am::phase_times;
    double total = p.inflate + p.unpack + p.walk + p.snapshots;
    std::printf("%zu games, %.1f ms each: gunzip %.1f, unpack %.1f, walk %.1f, snapshots %.1f\n", blobs.size(),
                1000 * total / blobs.size(), 1000 * p.inflate / blobs.size(), 1000 * p.unpack / blobs.size(),
                1000 * p.walk / blobs.size(), 1000 * p.snapshots / blobs.size());
    return 0;
  }
  int threads = (int)std::thread::hardware_concurrency();
  for (size_t i = 0; i < args.size(); i++)
    if (args[i] == "-j" && i + 1 < args.size()) threads = std::max(1, std::stoi(args[i + 1]));
  if (args.size() == 1 && (args[0] == "-" || args[0][0] != '-')) {   // "-": the replay on stdin
    try {
      std::string bytes;
      if (args[0] == "-") {
        std::ostringstream ss;
        ss << std::cin.rdbuf();
        bytes = ss.str();
      } else {
        bytes = slurp(args[0]);
      }
      std::cout << am::to_json(am::compute(bytes)) << "\n";
      return 0;
    } catch (std::exception& e) {
      std::cerr << "arena-metrics: " << args[0] << ": " << e.what() << "\n";
      return 1;
    }
  }
  std::vector<std::pair<std::string, std::string>> jobs;
  for (std::string line; std::getline(std::cin, line);) {
    auto tab = line.find('\t');
    if (tab != std::string::npos) jobs.push_back({line.substr(0, tab), line.substr(tab + 1)});
  }
  std::atomic<size_t> next{0};
  std::mutex out_mu;
  auto work = [&] {
    for (size_t k; (k = next++) < jobs.size();) {
      const auto& [in, out] = jobs[k];
      std::string json, err;
      try {
        json = am::to_json(am::compute(slurp(in)));
      } catch (std::exception& e) {
        err = e.what();
        json = "{\"error\": \"native: " + escape(err) + "\"}";
      }
      std::string tmp = out + ".tmp" + std::to_string(getpid()) + "-" + std::to_string(k);
      bool wrote = false;
      {
        std::ofstream f(tmp, std::ios::binary);
        if (f) {
          f << json;
          wrote = bool(f);
        }
      }
      if (wrote) wrote = std::rename(tmp.c_str(), out.c_str()) == 0;
      else std::remove(tmp.c_str());
      std::lock_guard<std::mutex> lk(out_mu);
      if (!wrote) std::cout << "error " << in << ": can't write " << out << "\n";
      else if (!err.empty()) std::cout << "error " << in << ": " << err << "\n";
      else std::cout << "ok " << in << "\n";
    }
  };
  std::vector<std::thread> pool;
  for (int i = 0; i < std::min<int>(threads, (int)jobs.size()); i++) pool.emplace_back(work);
  for (auto& t : pool) t.join();
  std::cout.flush();
  return 0;
}
