// Exercise the real CLI parser and JSON serializer without loading Linux BPF.
#include "micro_exec.hpp"

#include <exception>
#include <iostream>
#include <string_view>

int main(int argc, char **argv)
{
    try {
        if (argc == 2 && std::string_view(argv[1]) == "serialize-counts") {
            sample_result sample;
            sample.measured_iterations = UINT32_MAX;
            sample.warmup_batches = UINT32_MAX;
            sample.warmup_iterations = UINT64_C(18446744065119617025);
            sample.timing_source = "ktime";
            sample.timing_source_wall = "clock_monotonic";
            sample.exec_cycles = 123;
            sample.exec_cycles_source = "tsc_ticks";
            sample.exec_cycles_scope = "test_run_syscall_per_iteration";
            print_json(std::cout, sample);
            return 0;
        }
        const auto options = parse_args(argc, argv);
        std::cout << "{\"repeat\":" << options.repeat
                  << ",\"warmup_repeat\":" << options.warmup_repeat << "}\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << error.what() << "\n";
        return 1;
    }
}
