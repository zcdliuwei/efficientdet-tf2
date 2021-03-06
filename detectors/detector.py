import tensorflow as tf
from abc import ABCMeta
from abc import abstractmethod

from necks import build_neck
from backbones import build_backbone
from core.layers import CombinedNonMaxSuppression


class Detector(metaclass=ABCMeta):
    def __init__(self, cfg, head, **kwargs):
        self.cfg = cfg

        self.head = head

        self.model = self.build_model()
        self.model.summary()
        if ".h5" in cfg.pretrained_weights_path:
            self.model.load_weights(cfg.pretrained_weights_path, by_name=True)
            tf.print("Restored pre-trained weights from %s" % cfg.pretrained_weights_path)
        else:
            self.init_weights(cfg.pretrained_weights_path)
            tf.print("Restored pre-trained weights from %s" % cfg.pretrained_weights_path)

        self.nms = CombinedNonMaxSuppression(iou_threshold=cfg.postprocess.iou_threshold,
                                             score_threshold=cfg.postprocess.score_threshold,
                                             pre_nms_size=cfg.postprocess.pre_nms_size,
                                             post_nms_size=cfg.postprocess.post_nms_size,
                                             num_classes=cfg.num_classes)
    
    @property
    def num_classes(self):
        return self.cfg.head.num_classes

    @abstractmethod 
    def init_weights(self, pretrained_weight_path=None):
        raise NotImplementedError()
  
    def build_model(self):
        inputs = tf.keras.Input(list(self.cfg.train.dataset.input_size) + [3], name="inputs")
        outputs = build_backbone(self.cfg.backbone.backbone,
                                 convolution=self.cfg.backbone.convolution,
                                 normalization=self.cfg.backbone.normalization.as_dict(),
                                 activation=self.cfg.backbone.activation,
                                 output_indices=self.cfg.backbone.output_indices,
                                 strides=self.cfg.backbone.strides,
                                 dilation_rates=self.cfg.backbone.dilation_rates,
                                 frozen_stages=self.cfg.backbone.frozen_stages,
                                 weight_decay=self.cfg.backbone.weight_decay,
                                 dropblock=self.cfg.backbone.dropblock,
                                 pretrained_weights_path=self.cfg.train.pretrained_weights_path,
                                 input_tensor=inputs,
                                 input_shape=self.cfg.train.dataset.input_size + [3]) 

        if self.cfg.neck is not None:
            outputs = build_neck(self.cfg.neck.neck,
                                 inputs=outputs,
                                 convolution=self.cfg.neck.convolution,
                                 normalization=self.cfg.neck.normalization,
                                 activation=self.cfg.neck.activation,
                                 feat_dims=self.cfg.neck.feat_dims,
                                 anchor_strides=self.cfg.neck.anchor_strides,
                                 weight_decay=self.cfg.neck.weight_decay,
                                 add_extra_conv=self.cfg.neck.add_extra_conv,
                                 use_multiplication=self.cfg.neck.use_multiplication).build_model()

        outputs = self.head.build_head(outputs)
        return tf.keras.Model(inputs=inputs, outputs=outputs, name=self.cfg.detector)

    def _get_matched_gt_boxes(self, target_boxes, mask):
        with tf.name_scope("get_matched_gt_boxes"):
            matched_boxes_ta = tf.TensorArray(size=0, dynamic_size=True, dtype=target_boxes.dtype)
            max_size = 800
            for i in tf.range(tf.shape(target_boxes)[0]):
                valid_boxes = tf.boolean_mask(target_boxes[i], mask[i])
                size = tf.shape(valid_boxes)[0]
                if tf.greater_equal(size, max_size):
                    matched_boxes = valid_boxes[:max_size]
                else:
                    matched_boxes = tf.concat([valid_boxes, tf.zeros([max_size - size, 4], dtype=target_boxes.dtype)], 0)

                matched_boxes_ta = matched_boxes_ta.write(i, matched_boxes)

            return matched_boxes_ta.stack()

    def summary_boxes(self, outputs, image_info):
        with tf.name_scope("summary_boxes"):
            predicted_boxes = tf.cast(outputs["predicted_boxes"], tf.float32)
            predicted_labels = tf.cast(outputs["predicted_labels"], tf.float32)
            target_boxes = image_info["target_boxes"]
            target_labels = image_info["target_labels"]
            total_anchors = image_info["total_anchors"]
     
            predicted_boxes = self.delta2box(total_anchors, predicted_boxes)
            matched_gt_boxes = self._get_matched_gt_boxes(target_boxes, target_labels >= 1)
            input_size = image_info["input_size"]
            predicted_boxes *= (1. / input_size)
            matched_gt_boxes *= (1. / input_size)
        
            if self.cfg.use_sigmoid:
                predicted_scores = tf.nn.sigmoid(predicted_labels)
            else:
                predicted_scores = tf.nn.softmax(predicted_labels, axis=-1)
                predicted_scores = predicted_scores[:, :, 1:]
            nmsed_boxes, nmsed_scores, nmsed_classes, _ = self.nms(predicted_boxes, predicted_scores)

            return matched_gt_boxes, nmsed_boxes, nmsed_scores, nmsed_classes

    def losses(self, outputs, image_info):
        with tf.name_scope("losses"):
            result = self.head.compute_losses(outputs, image_info)

            # l2_loss = tf.add_n(self.model.losses)
            l2_loss = self.cfg.weight_decay * tf.add_n(
                [tf.nn.l2_loss(variable) for variable in self.model.trainable_variables if "kernel" in variable.name])
            
            result["l2_loss"] = l2_loss

            result["loss"] = tf.add_n([v for k, v in result.items()])

            return result
