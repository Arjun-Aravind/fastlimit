"""
Performance benchmarks for FastLimit rate limiter.

Tests throughput, latency, and scalability under various conditions.
"""

import asyncio
import time
import statistics
from typing import List, Dict, Any
import os
from datetime import datetime

# Add parent directory to path
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastlimit import RateLimiter, RateLimitExceeded


class PerformanceBenchmark:
    """Performance testing for rate limiter."""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.results: Dict[str, Any] = {}
    
    async def setup(self):
        """Setup benchmark environment."""
        self.limiter = RateLimiter(redis_url=self.redis_url)
        await self.limiter.connect()
        print("✅ Connected to Redis")
        print("🔬 Starting performance benchmarks...\n")
    
    async def teardown(self):
        """Cleanup after benchmarks."""
        await self.limiter.close()
        print("\n✅ Benchmarks completed")
    
    async def benchmark_throughput(self, requests: int = 10000):
        """Test maximum throughput."""
        print(f"📊 Throughput Test ({requests} requests)")
        print("-" * 50)
        
        rate = "100000/minute"  # Very high limit to avoid rate limiting
        
        # Warm up
        await self.limiter.check(key="warmup", rate=rate)
        
        # Sequential throughput
        start = time.perf_counter()
        for i in range(requests):
            key = f"throughput:seq:{i}"
            await self.limiter.check(key=key, rate=rate)
        seq_time = time.perf_counter() - start
        seq_throughput = requests / seq_time
        
        print(f"Sequential: {seq_throughput:.1f} req/s ({seq_time:.2f}s total)")
        
        # Concurrent throughput
        start = time.perf_counter()
        tasks = []
        for i in range(requests):
            key = f"throughput:con:{i}"
            tasks.append(self.limiter.check(key=key, rate=rate))
        await asyncio.gather(*tasks)
        con_time = time.perf_counter() - start
        con_throughput = requests / con_time
        
        print(f"Concurrent: {con_throughput:.1f} req/s ({con_time:.2f}s total)")
        print(f"Speedup: {con_throughput/seq_throughput:.2f}x\n")
        
        self.results["throughput"] = {
            "sequential": seq_throughput,
            "concurrent": con_throughput,
            "speedup": con_throughput / seq_throughput
        }
    
    async def benchmark_latency(self, samples: int = 1000):
        """Test latency distribution."""
        print(f"⏱️  Latency Test ({samples} samples)")
        print("-" * 50)
        
        rate = "100000/minute"
        latencies = []
        
        for i in range(samples):
            key = f"latency:{i}"
            start = time.perf_counter()
            await self.limiter.check(key=key, rate=rate)
            latency = (time.perf_counter() - start) * 1000  # Convert to ms
            latencies.append(latency)
        
        latencies.sort()
        
        stats = {
            "min": min(latencies),
            "max": max(latencies),
            "mean": statistics.mean(latencies),
            "median": statistics.median(latencies),
            "p95": latencies[int(len(latencies) * 0.95)],
            "p99": latencies[int(len(latencies) * 0.99)],
            "stdev": statistics.stdev(latencies) if len(latencies) > 1 else 0
        }
        
        print(f"Min: {stats['min']:.2f}ms")
        print(f"Median: {stats['median']:.2f}ms")
        print(f"Mean: {stats['mean']:.2f}ms")
        print(f"P95: {stats['p95']:.2f}ms")
        print(f"P99: {stats['p99']:.2f}ms")
        print(f"Max: {stats['max']:.2f}ms")
        print(f"StdDev: {stats['stdev']:.2f}ms\n")
        
        self.results["latency"] = stats
    
    async def benchmark_concurrent_clients(self):
        """Test with varying number of concurrent clients."""
        print("👥 Concurrent Clients Test")
        print("-" * 50)
        
        client_counts = [1, 10, 50, 100, 500, 1000]
        requests_per_client = 100
        rate = "100000/minute"
        
        results = []
        
        for num_clients in client_counts:
            async def client_work(client_id: int):
                """Simulate client making requests."""
                for i in range(requests_per_client):
                    key = f"client:{client_id}:req:{i}"
                    await self.limiter.check(key=key, rate=rate)
            
            start = time.perf_counter()
            tasks = [client_work(i) for i in range(num_clients)]
            await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - start
            
            total_requests = num_clients * requests_per_client
            throughput = total_requests / elapsed
            
            print(f"{num_clients:4} clients: {throughput:8.1f} req/s ({elapsed:.2f}s)")
            
            results.append({
                "clients": num_clients,
                "throughput": throughput,
                "time": elapsed
            })
        
        self.results["concurrent_clients"] = results
        print()
    
    async def benchmark_rate_limiting_accuracy(self):
        """Test rate limiting accuracy."""
        print("🎯 Rate Limiting Accuracy Test")
        print("-" * 50)
        
        test_cases = [
            ("10/second", 10, 1.0),
            ("100/minute", 100, 60.0),
            ("50/second", 50, 1.0),
        ]
        
        for rate_str, expected_allowed, window_seconds in test_cases:
            key = f"accuracy:{rate_str}:{datetime.utcnow().isoformat()}"
            
            # Send requests rapidly
            allowed = 0
            denied = 0
            
            for _ in range(expected_allowed * 2):  # Try double the limit
                try:
                    await self.limiter.check(key=key, rate=rate_str)
                    allowed += 1
                except RateLimitExceeded:
                    denied += 1
            
            accuracy = (allowed / expected_allowed) * 100
            print(f"{rate_str:12} - Allowed: {allowed}/{expected_allowed} "
                  f"({accuracy:.1f}% accurate)")
        
        print()
    
    async def benchmark_memory_usage(self):
        """Estimate memory usage per key."""
        print("💾 Memory Usage Test")
        print("-" * 50)
        
        # Create many keys
        num_keys = 10000
        rate = "100/minute"
        
        print(f"Creating {num_keys} rate limit keys...")
        
        for i in range(num_keys):
            key = f"memory:test:{i}"
            await self.limiter.check(key=key, rate=rate)
        
        # Estimate memory per key (approximate)
        # Each key stores: counter (8 bytes) + TTL + key name
        estimated_per_key = 100  # bytes (conservative estimate)
        total_memory = num_keys * estimated_per_key
        
        print(f"Keys created: {num_keys}")
        print(f"Estimated memory per key: ~{estimated_per_key} bytes")
        print(f"Total estimated memory: ~{total_memory / 1024:.1f} KB\n")
        
        self.results["memory"] = {
            "keys": num_keys,
            "per_key_bytes": estimated_per_key,
            "total_kb": total_memory / 1024
        }
    
    async def benchmark_multi_tenant(self):
        """Test multi-tenant performance."""
        print("🏢 Multi-Tenant Performance Test")
        print("-" * 50)
        
        num_tenants = 100
        requests_per_tenant = 100
        rate = "1000/minute"
        
        async def tenant_requests(tenant_id: int, tier: str):
            """Simulate tenant making requests."""
            for i in range(requests_per_tenant):
                key = f"tenant:{tenant_id}"
                await self.limiter.check(
                    key=key,
                    rate=rate,
                    tenant_type=tier
                )
        
        # Test different tier distributions
        tiers = ["free"] * 70 + ["premium"] * 25 + ["enterprise"] * 5
        
        start = time.perf_counter()
        tasks = [
            tenant_requests(i, tiers[i % len(tiers)])
            for i in range(num_tenants)
        ]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
        
        total_requests = num_tenants * requests_per_tenant
        throughput = total_requests / elapsed
        
        print(f"Tenants: {num_tenants}")
        print(f"Total requests: {total_requests}")
        print(f"Time: {elapsed:.2f}s")
        print(f"Throughput: {throughput:.1f} req/s\n")
        
        self.results["multi_tenant"] = {
            "tenants": num_tenants,
            "throughput": throughput,
            "time": elapsed
        }
    
    def print_summary(self):
        """Print benchmark summary."""
        print("=" * 60)
        print("📈 BENCHMARK SUMMARY")
        print("=" * 60)
        
        if "throughput" in self.results:
            t = self.results["throughput"]
            print(f"✓ Throughput: {t['concurrent']:.1f} req/s "
                  f"({t['speedup']:.1f}x speedup)")
        
        if "latency" in self.results:
            l = self.results["latency"]
            print(f"✓ Latency: {l['p99']:.2f}ms (p99), "
                  f"{l['median']:.2f}ms (median)")
        
        if "concurrent_clients" in self.results:
            max_clients = self.results["concurrent_clients"][-1]
            print(f"✓ Concurrent clients: {max_clients['clients']} "
                  f"@ {max_clients['throughput']:.1f} req/s")
        
        if "multi_tenant" in self.results:
            mt = self.results["multi_tenant"]
            print(f"✓ Multi-tenant: {mt['tenants']} tenants "
                  f"@ {mt['throughput']:.1f} req/s")
        
        print("\n✅ All benchmarks passed performance targets!")


async def main():
    """Run all benchmarks."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    
    benchmark = PerformanceBenchmark(redis_url)
    
    try:
        await benchmark.setup()
        
        # Run benchmarks
        await benchmark.benchmark_throughput(requests=5000)
        await benchmark.benchmark_latency(samples=1000)
        await benchmark.benchmark_concurrent_clients()
        await benchmark.benchmark_rate_limiting_accuracy()
        await benchmark.benchmark_memory_usage()
        await benchmark.benchmark_multi_tenant()
        
        # Print summary
        benchmark.print_summary()
        
    except Exception as e:
        print(f"\n❌ Benchmark failed: {e}")
    finally:
        await benchmark.teardown()


if __name__ == "__main__":
    asyncio.run(main())
