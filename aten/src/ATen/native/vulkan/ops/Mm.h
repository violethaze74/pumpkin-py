#pragma once

#ifdef USE_VULKAN_API

#include <ATen/native/vulkan/ops/Common.h>
#include <ATen/native/vulkan/ops/VulkanPackedContext.h>
#include <torch/library.h>

namespace at {
namespace native {
namespace vulkan {
namespace ops {

class LinearPackedContext final : virtual public VulkanPackedContext,
                                  public torch::jit::CustomClassHolder {
 private:
  c10::impl::GenericList unpacked_;

 public:
  LinearPackedContext(const Tensor& weight, const c10::optional<Tensor>& bias);

  static LinearPackedContext pack(c10::impl::GenericList);

  const c10::impl::GenericList unpack() const override {
    return unpacked_;
  }
};

c10::intrusive_ptr<LinearPackedContext> create_linear_context(
    Tensor&& weight,
    c10::optional<Tensor>&& bias);

Tensor run_linear_context(
    const Tensor& input,
    const c10::intrusive_ptr<LinearPackedContext>& context);

} // namespace ops
} // namespace vulkan
} // namespace native
} // namespace at

#endif /* USE_VULKAN_API */
